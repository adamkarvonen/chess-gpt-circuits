#!/bin/bash

# Check if unzip is installed, and exit if it isn't
if ! command -v unzip &> /dev/null
then
    echo "Error: unzip is not installed. Please install it and rerun the setup script."
    exit 1
fi

pip install -r requirements.txt
pip install -e .
git submodule update --init

cd models
wget -O lichess_8layers_ckpt_no_optimizer.pt "https://huggingface.co/adamkarvonen/chess_llms/resolve/main/lichess_8layers_ckpt_no_optimizer.pt?download=true"

cd ..

cd autoencoders

wget -O group0.zip "https://huggingface.co/adamkarvonen/chess_saes/resolve/main/group0.zip?download=true"
unzip group0.zip
rm group0.zip

cd ..

cd circuits
cd dictionary_learning

git checkout collab
git pull