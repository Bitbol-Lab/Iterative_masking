# Iterative_masking
> Supporting repository for: "Generative power of a protein language model trained on


## Getting started

Clone this repository on your local machine by running:

```bash
git clone git@github.com:Bitbol-Lab/Iterative_masking.git
```
and move inside the root folder.
We recommend creating and activating a dedicated ``conda`` or ``virtualenv`` Python virtual environment.

## Requirements
In order to run the notebooks, the following python packages are required:

- numpy
- numba
- fastcore
- biopython
- esm==0.4.0
- pytorch
- cuda

## How to use

`IM_MSA_Transformer`: Class with different functions used to generate new MSAs with the iterative masking procedure

`gen_MSAs`: example function (with parser) that can be used to generate and save new sequences directly from the terminal.

