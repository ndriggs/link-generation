from torch.utils.data import Dataset
from torch.nn.utils.rnn import pad_sequence
from torch_geometric.data import Data
from torch_geometric.loader import DataLoader
import torch
import numpy as np
from typing import Union
import json

def load_braid_words(train_test_or_val: str):
    with open(f'src/rl_link_builder/predicting_signature/{train_test_or_val}_braids.txt', 'r') as f :
        braid_words = json.load(f)
    return braid_words

def remove_cancelations(train_test_or_val: str) :
    '''Removes all adjacent inverses from all braid words in the list
    including ones at the front and back
    e.g. [1,-3,3,2,1] becomes [1,2,1] and [2,1,-3,-2] becomes [1,-3]
    
    returns: list of lists (list of braid words)'''
    braid_words = load_braid_words(train_test_or_val)
    for i, braid_word in enumerate(braid_words) :
        stack = []
        # remove all adjacent inverses
        for generator in braid_word:
            if stack and stack[-1] + generator == 0:
                stack.pop()
            else:
                stack.append(generator)
        # remove all inverses at the front and back
        if len(stack) > 1 :
            while stack[0] + stack[-1] == 0 :
                stack = stack[1:-1]
        if len(stack) > 1 :
            braid_words[i] = stack
    
    return braid_words


def pad_braid_words(braid_words, pad_value=0):

    # get lengths of braid words
    lengths = [len(braid_word) for braid_word in braid_words]

    # convert the generators to tokens
    # sigma_{1} = 1, sigma_{6} = 6, simga_{-1} = 7, sigma_{-2} = 8, etc.
    braid_index = 7
    for i, braid_word in enumerate(braid_words) : 
        braid_word = np.array(braid_word)
        braid_word = np.abs(braid_word) + (1-np.sign(braid_word))*((braid_index-1)/2)
        braid_words[i] = braid_word

    # convert braid words to torch tensors
    tensor_seqs = [torch.LongTensor(braid_word) for braid_word in braid_words]
    
    # Pad braid words to be the same length
    padded_seqs = pad_sequence(tensor_seqs, batch_first=True, padding_value=pad_value)
    
    # Create a tensor of sequence lengths
    lengths_tensor = torch.LongTensor(lengths)
    
    return padded_seqs, lengths_tensor

def braid_word_to_geom_data(braid_word, y, ohe_inverses: bool) :
    '''Converts a braid word (as a list of integers) to a torch_geometric.data.Data object
    containing node features and adjacency list information

    parameters: 
    braid_word: braid word as a list 

    y: the target invariant value(s)

    ohe_inverses: if True then one-hot-encodes all generators for node features. If False 
    then only one-hot-encodes the positive sigmas and uses -1 in the appropriate spot for 
    their inverses. e.g. if the braid index was 4 and ohe_inverses = True, then sigma_1 = 
    [1,0,0,0,0,0] and sigma_{-1} = [0,0,0,1,0,0]. If ohe_inverses = False then sigma_1 = 
    [1,0,0] and sigma_{-1} = [-1,0,0]'''

    # initialize edges with the edge between the first and last generator in the braid word
    edges = [[0, len(braid_word)-1], [len(braid_word)-1, 0]]
    # add an edge between all adjacent spots in the braid word
    if len(braid_word) > 2 :
        for i in range(len(braid_word)-1) :
            edges.append([i, i+1])
            edges.append([i+1, i])

    # construct the node features, with each generator in the braid word as a node
    braid_word = torch.tensor(braid_word)
    braid_index = 7
    if ohe_inverses :
        # move the negative sigmas to the spots after the positive ones
        # convert -1 to 7, -2 to 8, -3 to 9, etc. 
        braid_word = torch.abs(braid_word) + (1-torch.sign(braid_word))*((braid_index-1)/2)
        node_features = torch.zeros((len(braid_word),(braid_index-1)*2))
        node_features[torch.arange(len(braid_word)),braid_word-1] = 1.0
    else : 
        node_features = torch.zeros((len(braid_word),braid_index-1))
        node_features[torch.arange(len(braid_word)),torch.abs(braid_word)-1] = torch.sign(braid_word).to(torch.float32)

    # now return the graph Data object
    return Data(x=node_features, edge_index=torch.LongTensor(edges).t(), y=torch.tensor(y))

def get_graph_dataloader(braid_words, targets, ohe_inverses:bool, batch_size:int, shuffle:bool) :
    data_list = [braid_word_to_geom_data(braid_word, y, ohe_inverses) for braid_word, y in zip(braid_words, targets)]
    return DataLoader(data_list, batch_size=batch_size, shuffle=shuffle)


class BraidDataset(Dataset):
    def __init__(self, data: Union[np.ndarray, torch.Tensor], targets: np.ndarray, 
                 classification:bool, cnn:bool=False, 
                 seq_lengths=None, lk_matrix_size=21):

        if isinstance(data, torch.Tensor) : # braid word sequences
            self.data = data 
        else : # LK representation data 
            if cnn : # reshape to have dim (batch, channel, height, width)
                self.data = torch.from_numpy(data.reshape(-1,lk_matrix_size,lk_matrix_size)).float().unsqueeze(1)
            else :
                self.data = torch.from_numpy(data).float()
                
        if classification :
            self.targets = torch.from_numpy(targets).long()
        else : 
            self.targets = torch.from_numpy(targets).float()
        self.seq_lengths = seq_lengths # only use for transformer_encoder

    def __len__(self):
        return len(self.data)

    def __getitem__(self, idx):
        if self.seq_lengths is not None :
            return (self.data[idx], self.seq_lengths[idx]), self.targets[idx]
        else :
            return self.data[idx], self.targets[idx]


def get_experiment_name(args) :
    '''Returns a unique experiment name string based on hyperparameters'''
    model = args.model[:3]
    
    task = 'cls' if args.classification else 'reg'
    
    preproc = args.preprocessing 
    if preproc == 'remove_cancelations' :
        preproc = 'rm'
    elif preproc is None :
        preproc = ''
    
    if args.model == 'mlp' : 
        return f'{model}_{preproc}_{task}_h{args.hidden_size}_d{args.dropout}'

    elif args.model == 'cnn' :
        ln = 'ln' if args.layer_norm else ''
        return f'{model}_{preproc}_{task}_k{args.kernel_size}_{ln}'

    elif args.model in ['transformer_encoder', 'reformer'] :
        return f'{model}_{preproc}_{task}_d{args.d_model}_h{args.nheads}_l{args.num_layers}'

    elif args.model == 'gnn' :
        ohe = 'ohe' if args.ohe_inverses else 'neg'
        return f'{model}_{preproc}_{task}_l{args.num_layers}_{ohe}'
