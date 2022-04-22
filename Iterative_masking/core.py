# AUTOGENERATED! DO NOT EDIT! File to edit: 00_core.ipynb (unless otherwise specified).

__all__ = ['IM_MSA_Transformer', 'gen_MSAs']

# Cell

import numpy as np
import esm
from numba import njit, prange
import torch
from Bio import SeqIO
import itertools
from typing import List, Tuple
import string
from warnings import warn

torch.set_grad_enabled(False)


# Iterative masking MSA-Transformer
class IM_MSA_Transformer:
    """Class that implement the Iterative masking algorithm"""
    def __init__(self,
                 iterations=None,
                 p_mask=None,
                 filename=None,
                 num=None,
                 filepath=None):

        self.iterations = iterations    # number of iterations used to generate the MSA
        self.p_mask = p_mask            # masking probability for the MSA generation
        #---------------------------------------------------------------------------------------
        # Delete lowercase characters and punctuations from a string (input fasta file)
        self.deletekeys = dict.fromkeys(string.ascii_lowercase)
        self.deletekeys["."] = None
        self.deletekeys["*"] = None
        self.translation = str.maketrans(self.deletekeys)
        #---------------------------------------------------------------------------------------
        if filename is None or num is None or filepath is None:
            raise ValueError("`filepath`, `filename` and `num` must be specified to import the MSA")
        # Import Transformer model
        self.msa_transformer, self.msa_alphabet = esm.pretrained.esm_msa1b_t12_100M_UR50S()
        self.msa_transformer = self.msa_transformer.eval().cuda()
        self.msa_batch_converter = self.msa_alphabet.get_batch_converter()
        self.idx_list = self.msa_alphabet.tok_to_idx
        print('MSA Transformer model imported')

        # If filename is an array then it's the input MSA
        with torch.no_grad():
            if isinstance(filename,np.ndarray):
                self.msa_data = torch.Tensor(filename).type(torch.int64)
                if len(filename.shape) != 3:
                    raise ValueError("`filename` should be an array with 3 axes")
                self.msa_batch_tokens = self.msa_data[:, :num[0], :]
                print('Using MSA given in input')
            else:
                if len(num) != len(filename):
                    raise ValueError("`filename` and `num` must have the same length")
                #---------------------------------------------------------------------------------------
                # Import MSAs
                self.msa_data = []
                for ff, nn in zip(filename, num):
                    self.msa_data += [self.read_msa(filepath + '/' + ff, nn)]
                print('MSA Imported')
                #---------------------------------------------------------------------------------------
                # Create tokens starting from MSA
                self.msa_batch_labels, self.msa_batch_strs, self.msa_batch_tokens = self.msa_batch_converter(
                    self.msa_data)
                self.msa_data = (self.msa_batch_tokens).clone()
                print(f'We are using batch MSAs of {num[0]} sequences')
                self.msa_batch_tokens = self.msa_batch_tokens[:, :num[0], :]

            # Import tokens into cuda
            self.msa_batch_tokens = self.msa_batch_tokens.cuda()

            print('MSA converted into tokens tensor of size and type:')
            print(self.msa_batch_tokens.size(), self.msa_batch_tokens.dtype)

    #---------------------------------------------------------------------------------------
    # Useful functions for handling string sequences
    def read_sequence(self, filename: str) -> Tuple[str, str]:
        """ Reads the first (reference) sequences from a fasta or MSA file."""
        record = next(SeqIO.parse(filename, "fasta"))
        return record.description, str(record.seq)

    def remove_insertions(self, sequence: str) -> str:
        """ Removes any insertions into the sequence. Needed to load aligned sequences in an MSA. """
        return sequence.translate(self.translation)

    def read_msa(self, filename: str, nseq: int) -> List[Tuple[str, str]]:
        """ Reads the first nseq sequences from an MSA file, automatically removes insertions."""
        tot = len([elem.id for elem in SeqIO.parse(filename, "fasta")])
        print(f'Number of sequences in {filename}: ', tot)
        return [
            (record.description, self.remove_insertions(str(record.seq)))
            for record in itertools.islice(SeqIO.parse(filename, "fasta"), tot)]

#-----------------------------------------------------------------------------------------------------------------------
#                   USEFUL FUNCTIONS TO RUN THE MSA TRANSFORMER ON INFERENCE MODE
#-----------------------------------------------------------------------------------------------------------------------

    #-------------------------------------------------------------------------------------------------------------------
    def print_tokens(self, tokens=None):
        """
        Outputs (on the cpu) the input `tokens` of the MSA, detaching them from the GPU.
        """
        with torch.no_grad():
            if tokens is None:
                return ((self.msa_batch_tokens.detach().cpu()).to(
                    dtype=torch.int8)).numpy()
            else:
                return ((tokens.detach().cpu()).to(dtype=torch.int8)).numpy()

    #-------------------------------------------------------------------------------------------------------------------
    def compute_embeddings(self, tokens=None, lyrs=[12]):
        """
        Starting from the `tokens`, use the model to predict their output embeddings and their associated
        logits (when softmaxed they give the probability of each token)
        `lyrs`:       list of the layers from which extracting the embeddings (# 12 is the last layer)
        """
        with torch.no_grad():
            if tokens is None:
                tokens = self.msa_batch_tokens
            if not tokens.is_cuda:
                tokens = tokens.cuda()
            results = self.msa_transformer(tokens,
                                           repr_layers=lyrs,
                                           return_contacts=False)
            token_representations = results["representations"][lyrs[0]].detach().cpu().numpy()
            logits = results["logits"].detach().cpu().numpy()
        del results
        return token_representations, logits

    #-------------------------------------------------------------------------------------------------------------------
    def compute_contacts(self, tokens=None):
        """
        Starting from the `tokens`, use the model to predict the contact matrix of each MSA
        """
        with torch.no_grad():
            if tokens is None:
                tokens = self.msa_batch_tokens
            if not tokens.is_cuda:
                tokens = tokens.cuda()
            msa_contacts = self.msa_transformer.predict_contacts(tokens).cpu()
        return msa_contacts

    #-------------------------------------------------------------------------------------------------------------------
    @njit(parallel=True)
    def Weights_Phylogeny(tkn, delta=0.8):
        """
        Compute the Phylogeny weights of the sequences
        `tkn`:    the 2d array of tokens of one MSA, it should not have the first token (0)
                and it should end before the start of the padding tokens (1).
        `delta`:  the phylogeny parameter
        """
        depth, length = tkn.shape

        def _inner(seq1, seq2):
            return np.sum(seq1 != seq2) / length

        weights = np.empty(depth, dtype=np.float64)
        for i in prange(depth):
            dists = np.empty(depth, dtype=np.float64)
            for j in range(depth):
                dists[j] = _inner(tkn[i], tkn[j])
            within_neighbourhood = np.sum(dists < 1 - delta)
            weights[i] = 1 / within_neighbourhood
        return weights


#-----------------------------------------------------------------------------------------------------------------------
#                   USEFUL FUNCTIONS FOR THE MSA GENERATION WITH THE TRANSFORMER ON INFERENCE MODE
#-----------------------------------------------------------------------------------------------------------------------

#-------------------------------------------------------------------------------------------------------------------
# Softmax of the logits tensor

    def softmax_tensor(self, x, axis, T=1):
        """
        Compute softmax values for each sets of scores in `x` where `x` is the 4-d tensor of logits
        and `T` is the sampling temperature.
        """
        return torch.exp(x/T) / torch.sum(torch.exp(x/T), axis=axis)[:, :, :, None]

    #-------------------------------------------------------------------------------------------------------------------
    def generate_MSA(self, MSA_tokens, mask_idx=32, use_pdf=False, sample_all=False, T=1):
        """
        Generate a new MSA by masking some entries of the original MSA and
        re-predicting them through MSA Transformer.

        `MSA_tokens`: input tokens.

        `p_mask`:     probability that an entry of the MSA is masked.

        `mask_idx`:   masking index (as interpreted by the model), for MSA-Tr it's 32.

        `use_pdf`:    if it's True the function sample the token from the logits pdf
                    instead of getting the argmax (greedy sampling).

        `sample_all`: if True all the new tokens are obtained from the logits (both
                    the masked and the non masked), if False the non masked tokens
                    are left untouched and only the masked ones are changed.

        `T`:          Temperature of sampling from the pdf of output logits.
        """
        with torch.no_grad():
            if not MSA_tokens.is_cuda:
                MSA_tokens = MSA_tokens.cuda()
            mask = ((torch.rand(MSA_tokens.shape) > self.p_mask).type(
                torch.uint8)).cuda()
            masked_msa_tokens = MSA_tokens * mask + mask_idx * (1 - mask)
            results = self.msa_transformer(masked_msa_tokens,
                                           repr_layers=[12],
                                           return_contacts=False)
            msa_logits = self.softmax_tensor(x=results["logits"], axis=3, T=T)
            if use_pdf == False:
                new_msa_tokens = torch.argmax(msa_logits, dim=3)
            else:
                Vals = torch.tensor([
                    4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19,
                    20, 21, 22, 23, 30
                ],
                                    dtype=torch.int64)
                maxval = Vals[-1].cuda()
                msa_logits = msa_logits[:, :, :, Vals]
                msa_logits = msa_logits / (torch.sum(msa_logits,
                                                     axis=3)[:, :, :, None])
                cum = torch.cumsum(msa_logits, dim=3)
                idxs = torch.zeros_like(cum, dtype=torch.int64).cuda()
                idxs1 = Vals[None, None, None, :].cuda()
                idxs = idxs + idxs1
                sample = (torch.rand(
                    (cum.shape[0], cum.shape[1], cum.shape[2]))).cuda()
                idxs[torch.gt(sample[:, :, :, None], cum)] = 100
                new_msa_tokens = torch.minimum(torch.amin(idxs, axis=3),
                                               maxval)
                del cum, idxs, idxs1, sample
            if sample_all == False:
                  new_msa_tokens = MSA_tokens * mask + new_msa_tokens * (1 - mask)
            new_msa_tokens[:, :, 0] = 0
        del mask, masked_msa_tokens, results, msa_logits
        return new_msa_tokens


    #-------------------------------------------------------------------------------------------------------------------
    def NEW_MSA(self, use_pdf=False, simplified=False, sample_all=False, T=1):
        """
        Generate a new MSA by iteratively calling the masked MSA generator defined in: `self.generate_MSA`.

        ---> Use this function with `simplified`=False only if you need tokens in cuda ! (i.e. if you want to compute embed
             or contacs), otherwise use `simplified`=True.

        The variable `self.iterations` must be a numpy array which specifies when (at which iterations)
        the tokens should be saved. The last element of the array gives the maximum number of iterations that should be done.

        `use_pdf`:    if it's True the function sample the token from the logits pdf
                    instead of getting the argmax (greedy sampling).

        `sample_all`: if True all the new tokens are obtained from the logits (both
                    the masked and the non masked), if False the non masked tokens
                    are left untouched and only the masked ones are changed.

        `T`:          Temperature of sampling from the pdf of output logits.
        """
        if self.iterations is None or self.p_mask is None:
            raise ValueError(
                "Both `iterations` (numpy array) and `p_mask` (float) must be specified to generate a new MSA"
            )
        max_iter = self.iterations[-1]
        with torch.no_grad():
            new_msa_tokens = self.msa_batch_tokens.clone()
            all_tokens = torch.zeros(
                (len(self.iterations), self.msa_batch_tokens.shape[0],
                 self.msa_batch_tokens.shape[1],
                 self.msa_batch_tokens.shape[2]),
                dtype=torch.int64)
            if simplified:
                all_tokens = all_tokens.to(dtype=torch.int8)
            if self.msa_alphabet.mask_idx != 32:
                raise ValueError(
                    f"The token used for masking is {self.msa_alphabet.mask_idx} instead of 32"
                )
            # Iterate the MSA generation process
            j = 0
            for i in range(max_iter):
                new_msa_tokens = self.generate_MSA(
                    MSA_tokens=new_msa_tokens,
                    mask_idx=self.msa_alphabet.mask_idx,
                    use_pdf=use_pdf, sample_all=sample_all, T=T)
                if np.any((i + 1) == self.iterations):
                    # Save the tokens at the specified iterations
                    if simplified:
                        all_tokens[j,
                                   ...] = (new_msa_tokens.clone().detach().cpu()).to(
                                       dtype=torch.int8)
                    else:
                        all_tokens[j, ...] = new_msa_tokens.clone()
                    j += 1
        del new_msa_tokens
        if simplified:
            return all_tokens.numpy()
        else:
            return all_tokens.cuda()


    #-------------------------------------------------------------------------------------------------------------------
    def Batch_MSA(self, use_pdf=False, simplified=False, repetitions=2, sample_all=False, T=1, phylo=False):
        """
        Generate a full MSA by calling with different input MSAs the iterative MSA generator defined
        in: `self.NEW_MSA`.

        ---> Use this function with `simplified`=False only if you need tokens in cuda ! (i.e. if you want to compute embed
             or contacs), otherwise use `simplified`=True

        The variable `self.iterations` must be a numpy array which specifies when (at which iterations)
        the tokens must be saved. The last element of the array gives the maximum number of iterations that should be done.

        `repetitions`:      the number of times self.NEW_MSA() is repeated with a different input MSA.

        `use_pdf`:    if it's True the function sample the token from the logits pdf
                    instead of getting the argmax (greedy sampling).

        `sample_all`: if True all the new tokens are obtained from the logits (both
                    the masked and the non masked), if False the non masked tokens
                    are left untouched and only the masked ones are changed.

        `T`:          Temperature of sampling from the pdf of output logits.

        `phylo`:            if True the start sequences are sampled from phylogeny weights instead of randomly.
        """
        with torch.no_grad():
            all_tokens = np.zeros(
                (len(self.iterations), self.msa_batch_tokens.shape[0],
                 self.msa_batch_tokens.shape[1] * repetitions,
                 self.msa_batch_tokens.shape[2]),
                dtype=np.int64)
            if simplified:
                all_tokens = all_tokens.astype('int8')
            ALL_tokens = self.msa_data
            depth = self.msa_batch_tokens.shape[1]
            if repetitions * depth > ALL_tokens.shape[1]:
                all_tokens = np.zeros(
                    (len(self.iterations), self.msa_batch_tokens.shape[0],
                     ALL_tokens.shape[1], self.msa_batch_tokens.shape[2]),
                    dtype=np.int64)

            if not phylo:
                ALL_tokens = ALL_tokens[:, torch.randperm(ALL_tokens.shape[1]), :]
            else:
                _ = self.Weights_Phylogeny(ALL_tokens[0, :20, :], delta=0.8)
                phylo_w = self.Weights_Phylogeny(ALL_tokens[0, :, :], delta=0.8)
                indxs = torch.multinomial(phylo_w, ALL_tokens.shape[1], replacement=True)
                ALL_tokens = ALL_tokens[:, indxs, :]
            for i in range(repetitions):
                ind = torch.arange(i * depth, (i + 1) * depth)
                if (i + 1) * depth > ALL_tokens.shape[1]:
                    ind = torch.arange(i * depth, ALL_tokens.shape[1])
                self.msa_batch_tokens = ALL_tokens[:, ind, :]
                self.msa_batch_tokens = self.msa_batch_tokens.cuda()
                all_tokens[:, :,
                           ind.numpy(), :] = self.NEW_MSA(use_pdf=use_pdf, simplified=simplified, sample_all=sample_all, T=T)
                if (i + 1) * depth > ALL_tokens.shape[1]:
                    break

        if simplified:
            return (ALL_tokens[:, :repetitions *
                               depth, :].numpy()).astype('int8'), all_tokens
        else:
            return ALL_tokens[:, :repetitions *
                              depth, :], torch.from_numpy(all_tokens).cuda()


    #-------------------------------------------------------------------------------------------------------------------

    def generate_MSA_context(self, ancestor, context, mask_idx=32, use_pdf=False, sample_all=False, T=1):
        """
        Generate a new sequence by masking some entries of the original ancestor sequence and
        re-predicting them through the transformer model (mask only `ancestor`, not the `context`).

        `ancestor`:     input sequence to be masked iteratively.

        `context`:      context MSA (not masked).

        `p_mask`:       probability that an entry of the MSA is masked.

        `mask_idx`:     masking index (as interpreted by the model), for MSA-Tr it's 32.

        `use_pdf`:      if it's True the function sample the token from the logits pdf
                        instead of getting the argmax (greedy sampling).

        `sample_all`:   if True all the new tokens are obtained from the logits (both
                        the masked and the non masked), if False the non masked tokens
                        are left untouched and only the masked ones are changed.

        `T`:            Temperature of sampling from the pdf of output logits.
        """
        with torch.no_grad():

            if not ancestor.is_cuda:
                ancestor = ancestor.cuda()
            if not context.is_cuda:
                context = context.cuda()

            mask = ((torch.rand(ancestor.shape) > self.p_mask).type(torch.uint8)).cuda()
            masked_ancestor = ancestor * mask + mask_idx * (1 - mask)

            masked_msa_tokens = torch.zeros((context.shape[0],
                                             context.shape[1]+1,
                                             context.shape[2]),
                                             dtype=torch.int64).cuda()
            masked_msa_tokens[0, 0, :] = masked_ancestor
            masked_msa_tokens[:, 1:, :] = context

            results = self.msa_transformer(masked_msa_tokens,
                                           repr_layers=[12],
                                           return_contacts=False)
            results1 = results["logits"][:,0,:,:]
            results1 = results1[:,None,:,:]
            msa_logits = self.softmax_tensor(x=results1, axis=3, T=T)

            if use_pdf == False:
                new_generation = torch.argmax(msa_logits, dim=3)[0,0,:]
            else:
                Vals = torch.tensor([
                    4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19,
                    20, 21, 22, 23, 30],dtype=torch.int64)
                maxval = Vals[-1].cuda()
                msa_logits = msa_logits[:, :, :, Vals]
                msa_logits = msa_logits / (torch.sum(msa_logits,
                                                     axis=3)[:, :, :, None])
                cum = torch.cumsum(msa_logits, dim=3)
                idxs = torch.zeros_like(cum, dtype=torch.int64).cuda()
                idxs1 = Vals[None, None, None, :].cuda()
                idxs = idxs + idxs1
                sample = (torch.rand(
                    (cum.shape[0], cum.shape[1], cum.shape[2]))).cuda()
                idxs[torch.gt(sample[:, :, :, None], cum)] = 100
                new_generation = torch.minimum(torch.amin(idxs, axis=3),
                                               maxval)[0,0,:]
                del cum, idxs, idxs1, sample

            if sample_all == False:
                  new_generation = ancestor * mask + new_generation * (1 - mask)
            new_generation[0] = 0

        del mask, masked_msa_tokens, results, results1, msa_logits
        return new_generation

    #-------------------------------------------------------------------------------------------------------------------
    # Generate new sequence in a Linear tree by reiterating the function `generate_MSA_context()` starting from the sequence:
    # `ancestor` (original sequence) and using the sequences in `context` as context MSA.
    def Context_MSA(self, depth=None, ancestor=None, context=None, use_pdf=False, simplified=False, sample_all=False, print_all=True, T=1):
        """
        Generates a new MSA with context-generation by iterating the masking on the original ancestor sequence
        using: `self.generate_MSA_context`. It masks `ancestor` (original sequence) and uses the sequences in `context` as context MSA.

        ---> Use this function with `simplified`=False only if you need tokens in cuda ! (i.e. if you want to compute embed
             or contacs), otherwise use `simplified`=True

        The variable `self.iterations` must be a numpy array which specifies when (at which iterations)
        the tokens must be saved. The last element of the array gives the maximum number of iterations that should be done.
        If `print_all`=True then it saves the generated sequences at each iteration.

        `ancestor`:     input sequence to be masked iteratively.

        `context`:      context MSA (not masked).

        `use_pdf`:      if it's True the function sample the token from the logits pdf
                        instead of getting the argmax (greedy sampling).

        `sample_all`:   if True all the new tokens are obtained from the logits (both
                        the masked and the non masked), if False the non masked tokens
                        are left untouched and only the masked ones are changed.

        `T`:            Temperature of sampling from the pdf of output logits.

        `depth`:        number of generated sequences, if None the depth is the number of ancestor sequences.
        """
        with torch.no_grad():
            total_ran=False
            if ancestor is None and context is None and depth is not None:
                ALL_tokens = self.msa_data
                ALL_tokens = ALL_tokens[:, torch.randperm(ALL_tokens.shape[1]), :]
                ancestor = ALL_tokens[0,:depth,:]
                ALL_tokens = ALL_tokens[:, torch.randperm(ALL_tokens.shape[1]), :]
                context  = ALL_tokens[:,:self.msa_batch_tokens.shape[1],:]
            elif depth is None:
                depth = ancestor.shape[0]
                if isinstance(context,np.ndarray):
                    total_ran=False
                elif context=='tot-ran':
                    total_ran=True
            else:
                print('ERROR, either you give depth or you give ancestor and context')

            all_tokens = torch.zeros((self.msa_batch_tokens.shape[0],
                 self.iterations[-1]+1,
                 depth,
                 ancestor.shape[1]),
                dtype=torch.int64).cuda()

            ancestor = torch.from_numpy(ancestor).to(dtype=torch.int64)
            if not total_ran:
                context  = torch.from_numpy(context).to(dtype=torch.int64)
            if total_ran:
                ALL_tokens = self.msa_data

            all_tokens[0, 0, :, :] = ancestor

            if simplified:
                all_tokens = all_tokens.to(dtype=torch.int8)
            if self.msa_alphabet.mask_idx != 32:
                raise ValueError(
                    f"The token used for masking is {self.msa_alphabet.mask_idx} instead of 32"
                )

            # Iterate the MSA generation tree
            for j in range(depth):
                new_ancestor = all_tokens[0, 0, j, :]
                for i in range(1,self.iterations[-1]+1):
                    if total_ran:
                        context = (ALL_tokens[:, torch.randperm(ALL_tokens.shape[1])[:self.msa_batch_tokens.shape[1]], :]).cuda()
                    new_ancestor = self.generate_MSA_context(ancestor=new_ancestor,context=context, mask_idx=self.msa_alphabet.mask_idx, use_pdf=use_pdf, sample_all=sample_all, T=T)
                    if print_all:
                        all_tokens[0, i, j, :] = new_ancestor
                if not print_all:
                    all_tokens[0, -1, j, :] = new_ancestor
                # torch.cuda.empty_cache()

        if not print_all:
            all_tokens = all_tokens[:,torch.tensor([-1]),:,:]

        if simplified:
            return ((context.detach().cpu()).to(dtype=torch.int8)).numpy(), ((all_tokens.detach().cpu()).to(dtype=torch.int8)).numpy()
        else:
            return context.cuda(), all_tokens.cuda()


# Cell
import os
import pickle
from fastcore.script import *

@call_parse
def gen_MSAs(filepath:Param(help='Path of the input directory',type=str,default='./'),
         filename:Param(help='Name of the input file(s)',type=str,nargs='+',default=False),
         new_dir:Param(help='Name of the output directory',type=str,default=False),
         pdf:Param(help='Should I sample tokens from the pdf ? (bool)',type=bool_arg,default=False),
         T:Param(help='Which is the sampling Temperature from the pdf ? (only when `pdf` is True)',type=float,default=1),
         sample_all:Param(help='Should I sample all tokens or just the masked ones ? (True = sample all tokens)',type=bool_arg, default=False),
         Iters:Param(help='Number of total iterations to generate the new tokens',type=int,default=10),
         pmask:Param(help='Masking probability',type=float,default=0.1),
         num:Param(help='Size of the batches MSAs which the MSA-Transformer receives as input',type=int,nargs='+',default=100),
         depth:Param(help='Number of batches (of size num) that you want to generate',type=int,default=2),
         generate:Param(help='How should I generate sequences ? False (=Batch generation) or Linear with context (=linear-ran/linear-tot-ran), `-ran` means that the context MSA is sampled randomly (once) while `-tot-ran` means that it is sampled randomly each time.',type=str, default=False),
         print_all:Param(help='Should I print the MSA after each iteration ? (bool)',type=bool_arg,default=False),
         range_vals:Param(help='First and last index of the sequences that you want to use as ancestors', type=int,nargs='+',default=False),
         phylo_w:Param(help='Should I sample the starting sequences from the phylogeny weights ? (bool)',type=bool_arg,default=False)
         ):
    "Generate a new MSA either with Batch generation of Context generation. It shuffles the initial MSA and uses different slices as batch MSAs"

    # Create folder
    path = os.getcwd()
    path1 = new_dir
    if new_dir is False:
        path1 = filename[0][:-6]
    try:
        os.mkdir(path + "/" + path1)
    except OSError:
        print("Creation of the directory %s failed" % (path + "/" + path1))
    else:
        print("Successfully created the directory %s " % (path + "/" + path1))

    # Save Input MSA
    print('Tokenize')
    Class = IM_MSA_Transformer(filename=filename,
                               num=[-1],
                               filepath=filepath)
    idx_list = Class.idx_list
    old_tkn = Class.print_tokens()
    a_file = open(path1 + "/dictionary-tokens.pkl", "wb")
    pickle.dump(idx_list, a_file)
    a_file.close()
    np.save(path1 + "/original-tokens.npy", old_tkn[0])

    add_strs = ""
    if pdf==True:
        add_strs += f"_pdf(T={round(T,3)})"
        print(
            "We are sampling new tokens from the pdf of logits and not taking the mode of the pdf"
        )
    if T!=1 and pdf==False:
        print('To sample with a Temperature you should use pdf=True, otherwise the result is the same')
    if sample_all == False:
        add_strs += "_(only-masked-sampled)"
    if not generate==False:
        add_strs += "_"+generate+"_(context-"+str(num[0])+")"
    if phylo_w:
        add_strs += "_phylo-w"

    print('Generate Class')
    Class = IM_MSA_Transformer(iterations=np.array([Iters]),
                               p_mask=pmask,
                               filename=filename,
                               num=num,
                               filepath=filepath)

    print('Compute results from Class')
    Class.iterations = np.array([Iters])
    Class.p_mask = pmask

    if generate == False:
        print('Generating MSA with same size as the original one')
        old_T, new_T = Class.Batch_MSA(simplified=True,
                                    repetitions=depth,
                                    use_pdf=pdf, sample_all=sample_all, T=T, phylo=phylo_w)
        NNN = min(num[0] * depth, old_T.shape[1])

    elif generate=='linear-ran' or generate=='linear-tot-ran':
        print('Generate MSA with linear context generation')
        orig_tkn = np.load(path + "/" + path1 + "/original-tokens.npy")
        # select ancestor and context
        np.random.seed(0)
        indices = np.random.permutation(orig_tkn.shape[0])
        indexes_context = indices[:num[0]]
        indices = np.random.permutation(orig_tkn.shape[0])
        if depth == -1:
            ind_ancestor = indices
        elif range_vals is False:
            ind_ancestor = indices[:depth]
        else:
            if range_vals[1] == -1 :
                ind_ancestor = indices[range_vals[0]:]
                range_vals[1] = orig_tkn.shape[0]
            else:
                ind_ancestor = indices[range_vals[0]:range_vals[1]]
        ancestor = orig_tkn[ind_ancestor,:]
        context  = orig_tkn[indexes_context,:][None,:,:]
        if generate=='linear-tot-ran':
            context = 'tot-ran'
        old_T, new_T = Class.Context_MSA(None, ancestor, context, use_pdf=pdf, simplified=True, sample_all=sample_all, print_all=print_all, T=T)
        if generate=='linear-tot-ran':
            old_T = ancestor[None,:,:]
        NNN = new_T.shape[2]
    else:
        print('ERROR: Select a generative process')

    # define the name of the directory to be created and create it
    path2 = "Generated" + "_iter-" + str(
        Iters) + "_pmask-" + str(pmask) + "_seqs-" + str(NNN) + add_strs
    try:
        os.mkdir(path + "/" + path1 + "/" + path2)
    except OSError:
        print("Creation of the directory %s failed" % (path + "/" +
              path1 + "/" + path2))
    else:
        print("Successfully created the directory %s " % (path + "/" +
              path1 + "/" + path2))

    # Save data
    if generate == False or generate=='linear-tot-ran':
        np.save(path1 + "/" + path2 + "/shuffled-tokens.npy", old_T[0])
    else:
        np.save(path1 + "/" + path2 + "/context-tokens.npy", old_T[0])
    str_add = ''
    if range_vals is not False:
        str_add = '_range_indx_'+str(range_vals[0])+','+str(range_vals[1])
    np.save(path1 + "/" + path2 + "/new-tokens"+str_add+".npy", new_T[0])

    return 1