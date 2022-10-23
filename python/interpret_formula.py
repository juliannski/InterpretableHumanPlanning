import numpy as np
import operator
import pickle
import copy
import os
import argparse
from scipy.special import softmax
from IHP.learning_utils import get_normalized_feature_values, get_counts, \
                               pickle_load, get_normalized_features, create_dir
from IHP.modified_mouselab import TrialSequence
from RL2DT.MCRL.modified_mouselab_new import Trial as NewTrial
from RL2DT.interpret import wrapper_interpret
from RL2DT.hyperparams import *
from RL2DT.strategy_demonstrations import reward

def get_cluster_demo(trial, features, weights, normalized_features, inv_t=False,
                     verbose=True):
    """
    Perform a rollout of the softmax policy found with the EM algorithm. 
    
    Use max probability actions as positive examples for each consecutive state,
    and actions with lower pobabilities as negative examples.

    Parameters
    ----------
    trial : IHP.modified_mouselab.Trial
        Environment representing the Mouselab MDP used for generating states
    features : [ str ]
        Names of the features defined in the Mouselab MDP environment (Trial) used
        in the models of the EM clusters
    weights : [ float ]
        Weights associated with the features
    normalized_features : dict
        str : float
            Value for which the feature needs to be divided by to get a value in
            [0,1]
    inv_t (optional) : bool
        Whether to remove the last weight
    verbose (optional) : bool
        Whether to output info on the gathered positive/negative demonstrations
        
    Returns
    -------
    output_dict : dict
        (int since the Mouselab MDP has a discrete action set)
        pos : [ ( IHP.modified_mouselab.Trial, int ) ]
            State-action pairs generated by the softmax policy taking the most
            probable acfeaturetions in each step
        neg : [ ( IHP.modified_mouselab.Trial, int ) ]
            All other state-action pairs for actions not chosen in the previous
            step
    total_rew : int
        Termination reward for the rollout
    mean_greedy_mass : float
        Mean probability mass the highest-probability actions took considering
        all the states in the rollout
    """
    trial.reset_observations()
    dict_trial = NewTrial(trial.ground_truth, trial.structure_map, 
                          trial.max_depth, trial.reward_function)
    actions = []
    set_negative_clicks = []
    feature_len = weights.shape[0]
    beta = 1
    W = weights
    if inv_t:
        feature_len -= 1
        beta = weights[-1]
        W = weights[:-1]
    unobserved_nodes = trial.get_unobserved_nodes()
    click = -1
    
    max_val_mass = 0
    
    while(click != 0):
        unobserved_node_labels = [node.label for node in unobserved_nodes]
        feature_values = trial.get_node_feature_values(unobserved_nodes, features,
                                                       normalized_features)
        dot_product = beta*np.dot(W, feature_values.T)
        softmax_dot = softmax(dot_product)
        
        max_val = max(softmax_dot)
        n = sum([1 if i==max_val else 0 for i in softmax_dot])
        max_val_mass += n * max_val
        greedy_dot = [1./n if i==max_val else 0 for i in softmax_dot]
        
        click = np.random.choice(unobserved_node_labels, p = greedy_dot)
        negative_clicks = generate_negative_clicks(scope=unobserved_node_labels, 
                                                   probs=softmax_dot, good=click,
                                                   stochastic=False)
        negative_clicks.extend(actions.copy()) ## add observed nodes
        set_negative_clicks.append(negative_clicks)
        actions.append(click)
        if verbose:
            print('State: {}, neg examples: {}'.format([n.label 
                                                        for n in trial.observed_nodes],
                                                       negative_clicks))
        trial.node_map[click].observe()
        unobserved_nodes = trial.get_unobserved_nodes()
        
    positive_examples = list(STRATEGY(dict_trial, actions))
    negative_examples = []
    counter = -1
    for bad_clicks in set_negative_clicks:
        counter += 1
        curr_state = positive_examples[counter][0]
        for c in bad_clicks:
            negative_examples.append((curr_state, c))
        
    output_dict = {'pos': positive_examples, 'neg': negative_examples}
    _, rew = trial.get_termination_data()
    total_rew = rew - len(positive_examples) + 1 #assuming each click is worth 
        
    mean_greedy_mass = max_val_mass/len(actions)
    
    if verbose:
        print('Mean greedy actions mass: {}'.format(mean_greedy_mass))
    
    return output_dict, total_rew, mean_greedy_mass

def generate_negative_clicks(scope, probs, good, stochastic=False):
    """
    Generate a set of suboptimal actions.
    
    If stochastic, choose according to the (inverted) probability of taking the
    action. Otherwise, choose all the actions for whose the probability of being
    taken is non-maximum.

    Parameters
    ----------
    scope : [ int ]
        Actions to choose from
    probs : [ float ]
        Probabilities of generating each of the actions in scope
    good : int
        Chosen optimal action
    stochastic : bool
        Whether to generate suboptimal actions according to the probs or not
        
    Returns
    -------
    bad_actions : [ int ]
        Chosen suboptimal actions
    """
    if not(stochastic):
        bad_actions = []
        optim = max(probs)
        scope_probs = zip(scope, probs)
        for sc, pr in scope_probs:
            if pr < optim and sc != good:
                bad_actions.append(sc)
    else:
        inverted_probs = softmax([1-p for p in probs])
        bad_actions = []
        for i in range(len(scope)-1):
            bad_click = np.random.choice(scope, p=inverted_probs)
            if bad_click != good: bad_actions.append(bad_click)
        bad_actions = list(set(bad_actions))
    return bad_actions

def get_cluster_demos(pipeline, num_trials, weights, features, 
                      normalized_features, envs=None):
    """
    Perform multiple rollouts of the softmax policy found with the EM algorithm. 
    
    Save them in a properly curated dictionary.

    Parameters
    ----------
    pipeline : [ ( [ int ], function ) ]
        List of parameters used for creating the Mouselab MDP environments: 
        branching of the MDP and the reward function for specifying the numbers
        hidden by the nodes. For each rollout.
    num_trials : int
        Number of rollouts
    features : [ str ]
        Names of the features defined in the Mouselab MDP environment (Trial) used
        in the models of the EM clusters
    weights : [ float ]
        Weights associated with the features
    normalized_features : dict
        feature : float
            Value for which the feature needs to be divided by to get a value in
            [0,1]
    envs (optional) : [ [ int ] ] or None
        Exact reward hidden underneath the nodes for each Mouselab MDP environment
        (for each rollout). If None, the rewards are sampled using the pipeline
        
    Returns
    -------
    demonstrations : dict
        (int since the Mouselab MDP has a discrete action set)
        pos : [ ( IHP.modified_mouselab.Trial, int ) ]
            State-action pairs generated by the softmax policy taking the most
            probable actions in each step
        neg : [ ( IHP.modified_mouselab.Trial, int ) ]
            All other state-action pairs for actions not chosen in the previous
            step
        leng_pos : [ int ]
            Lengths of the consecutive rollouts
        leng_neg : [ int ]
            Lengths of negative examples for each rollout which follow the rollouts
            themselves
    """
    trials = TrialSequence(num_trials, pipeline, ground_truth=envs)
    demonstrations = {'pos': [], 'neg': [], 'leng_pos': [], 'leng_neg': []}
    iters = -1
    mean_masses = []
    for trial in trials.trial_sequence:
        iters += 1
        #print('Demos {}'.format(iters), end='\r')
        demo, _, mmass = get_cluster_demo(trial, features, weights, normalized_features)
        demonstrations['pos'].extend(demo['pos'])
        demonstrations['neg'].extend(demo['neg'])
        demonstrations['leng_pos'].append(len(demo['pos']))
        demonstrations['leng_neg'].append(len(demo['neg']))
        mean_masses.append(mmass)
    print('Mean action mass: {}, std: {}'.format(np.mean(mean_masses),
                                     np.std(mean_masses)))
    return demonstrations

def save_demos(demonstrations, info):
    """
    Save the demonstrations in a pickle file.

    Parameters
    ----------
    demonstrations : dict
        See get_cluster_demos
    info : str
        Additional text specifying the source for the demonstrations added to the
        demos file
        
    Returns
    -------
    demo_path : str
        Path to the demos file
    """
    cwd = os.getcwd()
    folder_path = cwd + "/demos/"
    
    demo_file = 'human_data_' + info
    demo_path = folder_path + demo_file + ".pkl"
    
    with open(demo_path, "wb") as filename:
        pickle.dump(demonstrations, filename)
    return demo_path
        
def estimate_cluster_reward(features, weights, normalized_features, iters=1000):
    """
    Estimate the mean reward achieved by the softmax policy by running it iters
    times.

    Parameters
    ----------
    (See get_cluster_demos)
    features : [ str ]
    weights : [ float ]
    normalized_features : dict
        str : float
    iters (optional) : int
        Number of iterations to run the softmax policy for
        
    Returns
    -------
    mean_rew : float
        Estimated mean reward
    """
    total_rew = 0
    
    def make_env(branching=BRANCHING, rew_func=reward):
        seq = TrialSequence(num_trials = 1, 
                            pipeline = [(branching, rew_func)])
        return seq.trial_sequence[0]

    for i in range(iters):
        print('Iteration {}/{}'.format(i,iters), end='\r')
        trial = make_env()
        _, rew, _ = get_cluster_demo(trial, features, weights, normalized_features,
                                     verbose=False)
        total_rew += rew
    mean_rew = total_rew / iters
    return mean_rew

def interpret_human_data(pipeline, weights, features, normalized_features, 
                         strategy_num, all_strategies, num_demos, exp_name, 
                         num_participants, max_divergence, size, tolerance, 
                         num_rollouts, num_samples, num_candidates, elbow_method,
                         candidate_clusters, mean_rew, expert_rew,
                         name_dsl_data=None, demo_path='', info=''):
    """
    Use AI-Interpret to find a DNF formula describing demonstrations produced by
    the softmax policy.
    
    Load or produce and save the demonstrations. Then find the mean reward of the
    softmax policy.
    
    Use demonstrations, mean reward and other parameters as input to AI-Interpret.

    Parameters
    ----------
    (See get_cluster_demos)
    pipeline : [ ( IHP.modified_mouselab.Trial, function ) ]
    features : [ str ]
    weights : [ float ]
    normalized_features : dict
        str : float
    strategy_num : int
        Identifier of the EM cluster (softmax policy) that is being interpreted
    all_strategies : int
        Identifier of the model that is the total number of EM clusters
    num_demos : int
        Number of demonstrations to produce
    exp_name : str
        Identifier of the experiment from which the human data (used to create 
        the clusters) came from
    num_participants : int
        Number of participants of exp_name whose data was considered
    max_divergence : float
        See max_divergence in RL2DT.interpret
    size : int
        See max_depth in RL2DT.interpret
    tolerance : float
        See tolerance in RL2DT.interpret
    num_rollouts : int
        See num_rollouts in RL2DT.interpret
    num_samples : int or float in [0,1]
        See num_samples in RL2DT.interpret
    num_candidates : int
        See num_candidates in RL2DT.interpret
    elbow_method : str
        See elbow_choice in RL2DT.interpret
    candidate_clusters : [ int ]
        See num_candidates in RL2DT.interpret
    mean_rew : float
        Mean reward achieved by the softmax policy; estimated if None
    expert_rew : float
        See mean_rew in RL2DT.interpret
    name_dsl_data (optional) : str
        See dsl_path in RL2DT.interpret
    demo_path (optional) : str
        Path to de pickle file with the demonstrations
    info (optional) : str
        See info in RL2DT.interpret
        
    Returns
    -------
    forml_statistics : dict
        str : dict
            See create_or_expand in RL2DT.interpret
    """
    add = info
    info = exp_name + '_' + str(strategy_num) + '_' + str(all_strategies) + '_' + \
           str(num_participants) + '_' + str(num_demos) + add
       
    assert (name_dsl_data != None and demo_path != '') or name_dsl_data == None, \
           "You provided DSL data but not the source demo_path"
    
    if demo_path == '':
        print('Getting demonstrations from the cluster...\n')
        demos = get_cluster_demos(pipeline, num_demos, weights, features,
                                  normalized_features)
        demo_path = save_demos(demos, info)
    else:
        with open(demo_path, 'rb') as handle:
            demos = pickle.load(handle)
            
    if not(mean_rew):
        print("Estimating the reward for the cluster's strategy...\n")
        reward = estimate_cluster_reward(features, weights, normalized_features,
                                         iters=num_rollouts)
        print("Estimated reward: {}\n".format(reward))
    else:
        reward = mean_rew
        print("Chosen reward: {}\n".format(mean_rew))
        
    
    forml_statistics = wrapper_interpret(algorithm='adaptive',
                                         num_demos=num_demos,
                                         env_name=exp_name,
                                         max_divergence=max_divergence,
                                         max_depth=size,
                                         tolerance=tolerance,
                                         demo_path=demo_path,
                                         demos=demos,
                                         expert_reward=reward,
                                         num_rollouts=num_rollouts,
                                         elbow_dsl_file=name_dsl_data,
                                         num_samples=num_samples,
                                         elbow_method=elbow_method,
                                         num_candidates=num_candidates,
                                         candidate_clusters=candidate_clusters,
                                         normalizer=expert_rew,
                                         info=info,
                                         stats=None,
                                         save=False)
    
    print('\n\n FORMULA:\n')
    for formula, data in forml_statistics.items():
        print('\n\n  {}:'.format(formula))
        for k,v in data.items():
            print('{}: {}\n'.format(k,v))
            
    cwd = os.getcwd()
    create_dir(cwd+'/interprets_formula')
    with open(cwd + '/interprets_formula/human_' + info + '.pkl', 'wb') as handle:
        pickle.dump(forml_statistics, handle)
            
    return forml_statistics

def load_EM_data(exp_num, num_strategies, num_participants, strategy_num,
                 num_simulations, info):
    """
    Load data pertinent to the softmax policies of the clusters found with the 
    EM algorithm.

    Parameters
    ----------
    exp_num : str
        Identifier of the experiment from which the human data (used to create 
        the clusters) came from
    num_strategies : int
        Number of the clusters that were computed
    num_participants : int
        Number of participants of exp_num whose data was considered
    strategy_num : int
        Identifier of the EM cluster (softmax policy) that is being interpreted
    num_simulations : int
        Number of policy demonstrations that are to be/were generated
        
    Returns
    -------
    pipeline : [ ( [ int ], function ) ]
        List of parameters used for creating the Mouselab MDP environments: 
        branching of the MDP and the reward function for specifying the numbers
        hidden by the nodes. For each rollout.
    features : [ str ]
        Names of the features defined in the Mouselab MDP environment (Trial) used
        in the models of the EM clusters
    weights_ : [ float ]
        Weights associated with the features
    normalized_features : dict
        str : float
            Value for which the feature needs to be divided by to get a value in
            [0,1]
    """
    clustered_weights_path = f"clustering/em_clustering_results/{exp_num}"
    file_name = f"{num_strategies}_{num_participants}_params" + info
    weights, _, _ =  pickle_load(f"{clustered_weights_path}/{file_name}.pkl")
    
    exp_pipelines = pickle_load("data/exp_pipelines.pkl")
    features = pickle_load("data/em_features.pkl")
    exp_reward_structures = pickle_load("data/exp_reward_structures.pkl")
    if exp_num == 'c2.1_dec':
        rew_struct = 'high_decreasing'
    else:
        rew_struct = exp_reward_structures[exp_num]
    normalized_features = get_normalized_features(rew_struct)
    pipeline = [exp_pipelines[exp_num][0]]*num_simulations
    weights_ = weights[strategy_num]
    
    return pipeline, weights_, features, normalized_features

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('--experiment_id', '-e',
                        type=str,
                        help="Identifier of the experiment to interpret.")
    parser.add_argument('--num_strategies', '-s',
                        type=int,
                        help="Number of strategies employed by the participants.")
    parser.add_argument('--num_participants', '-p',
                        type=int,
                        help="Number of participants whose data was taken into account.",
                        default=0)
    parser.add_argument('--strategy_num', '-c',
                        type=int,
                        help="Number of the strategy to interpret.")
    parser.add_argument('--demo_path', '-dp',
                        type=str,
                        help="Path to the file with the demonstrations.", 
                        default='')
    parser.add_argument('--num_demos', '-n', 
                        type=int,
                        help="How many demos to use for interpretation.")
    parser.add_argument('--elbow_choice', '-elb',
                        choices={'automatic','manual'},
                        help="Whether to find the candidates for the number of " \
                            +"clusters automatically or use the " \
                            +"candidate_clusters parameter.",
                        default='automatic')
    parser.add_argument('--mean_reward', '-mr',
                        type=float,
                        help="Mean reward the interpretation will aspire to.",
                        default=None)
    parser.add_argument('--expert_reward', '-er',
                        type=float,
                        help="Mean reward of the optimal strategy for the problem.")
    parser.add_argument('--candidate_clusters', '-cl',
                        type=int, nargs='+',
                        help="The candidate(s) for the number of clusters in " \
                            +"the data either to consider in their entirety " \
                            +"or to automatically choose from.",
                        default=NUM_CLUSTERS)
    parser.add_argument('--num_candidates', '-nc',
                        type=int,
                        help="The number of candidates for the number of " \
                            +"clusters to consider.",
                        default=NUM_CANDIDATES)
    parser.add_argument('--name_dsl_data', '-dsl', 
                        type=str,
                        help="Name of the .pkl file containing input demos turned " \
                            +"to binary vectors of predicates in folder demos", 
                        default=None)
    parser.add_argument('--interpret_size', '-i',
                        type=int,
                        help="Maximum depth of the interpretation tree",
                        default=MAX_DEPTH)
    parser.add_argument('--max_divergence', '-md',
                        type=float,
                        help="How close should the intepration performance in " \
                            +"terms of the reward be to the policy's performance",
                        default=MAX_DIVERGENCE)
    parser.add_argument('--tolerance', '-t',
                        type=float,
                        help="What increase in the percentage of the expected " \
                             "expert reward a formula is achieving is " \
                             "considered significant when comparing a two of them.",
                        default=TOLERANCE)
    parser.add_argument('--num_rollouts', '-rl',
                        type=int,
                        help="How many rolouts to perform to compute mean " \
                            +"return per environment",
                        default=NUM_ROLLOUTS)
    parser.add_argument('--samples', '-sm',
                        type=float,
                        help="How many samples/in what ratio to sample from clusters",
                        default=SPLIT)
    parser.add_argument('--info', '-f',
                        type=str,
                        help="What to add to the name of all the output files",
                        default='')

    args = parser.parse_args()
    
    res = load_EM_data(exp_num=args.experiment_id, 
                       num_strategies=args.num_strategies,
                       num_participants=args.num_participants,
                       strategy_num=args.strategy_num,
                       num_simulations=args.num_demos)
    pipeline, weights, features, normalized_features = res
    
    data = interpret_human_data(pipeline=pipeline, 
                                weights=weights, 
                                features=features,
                                normalized_features=normalized_features,
                                strategy_num=args.strategy_num, 
                                all_strategies=args.num_strategies,
                                num_demos=args.num_demos, 
                                exp_name=args.experiment_id, 
                                num_participants=args.num_participants, 
                                max_divergence=args.max_divergence, 
                                size=args.interpret_size, 
                                tolerance=args.tolerance, 
                                num_rollouts=args.num_rollouts, 
                                num_samples=args.samples, 
                                num_candidates=args.num_candidates, 
                                candidate_clusters=args.candidate_clusters, 
                                name_dsl_data=args.name_dsl_data, 
                                demo_path=args.demo_path,
                                elbow_method=args.elbow_choice,
                                mean_rew=args.mean_reward,
                                expert_rew=args.expert_reward,
                                info=args.info)
