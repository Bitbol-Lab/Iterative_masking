# Iterative_masking
> Supporting repository for: "Generative power of a protein language model trained on multiple sequence alignments" (preprint: https://doi.org/10.1101/2022.04.14.488405). We use MSA Transformer (https://doi.org/10.1101/2021.02.12.430858) to generate synthetic protein sequences by masking iteratively the same MSA.


## Getting started

Clone this repository on your local machine by running:

```bash
git clone git@github.com:Bitbol-Lab/Iterative_masking.git
```
and move inside the root folder.
One can the use directly the functions from the cloned repository or install it with an editable install using:

```bash
pip install -e .
```

We recommend creating and activating a dedicated ``conda`` or ``virtualenv`` Python virtual environment.

## Requirements
In order to use the functions, the following python packages are required:

- numpy
- scipy
- numba
- fastcore
- biopython
- esm==0.4.0
- pytorch

It is also required to use a GPU (with cuda).

## How to use

`IM_MSA_Transformer`: Class with different functions used to generate new MSAs with the iterative masking procedure

`gen_MSAs`: example function (with parser) that can be used to generate and save new sequences directly from the terminal.

