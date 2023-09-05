#!/bin/bash

# Define the name of the virtual environment folder
venv_folder="venv"

# Check if the virtual environment folder exists
if [ -d "$venv_folder" ]; then
    echo "Activating existing virtual environment..."
    source "$venv_folder/bin/activate"
else
    echo "Creating and activating a new virtual environment..."
    
    # Create a new virtual environment
    python3 -m venv "$venv_folder"
    
    # Activate the newly created virtual environment
    source "$venv_folder/bin/activate"
fi

# Verify that the virtual environment is activated
python --version

# Step 1/4: Installing Requirements
echo ""
echo "Step 1/4: Installing Requirements"
echo ""
pip install -r requirements-rocm.txt

# Step 2/4: Removing Unwanted bitsandbytes Installation
echo ""
echo "Step 2/4: Removing Unwanted bitsandbytes Installation"
echo ""
pip uninstall bitsandbytes

# Step 3/4: Cloning the bitsandbytes-rocm Fork's Repo
echo ""
echo "Step 3/4: Cloning the bitsandbytes-rocm Fork's Repo"
echo ""
cd venv
git clone https://github.com/arlo-phoenix/bitsandbytes-rocm-5.6.git bitsandbytes
cd bitsandbytes

# Step 4/4: Installing bitsandbytes-rocm
echo ""
echo "Step 4/4: Installing bitsandbytes-rocm"
echo ""
export ROCM_HOME=/opt/rocm-5.6.0
make hip ROCM_TARGET=gfx1100
pip install .
pip install scipy  # I think there's a requirements problem somewhere (at least in kohya_ss)
cd ..
cd ..
