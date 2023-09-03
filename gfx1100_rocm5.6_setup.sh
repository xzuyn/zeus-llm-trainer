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

# Step 1/5: Installing Requirements
echo ""
echo "Step 1/5: Installing Requirements"
echo ""
pip install -r requirements.txt

# Step 2/5: Removing Unwanted PyTorch and TorchVision Installations
echo ""
echo "Step 2/5: Removing Unwanted PyTorch and TorchVision Installations"
echo ""
pip uninstall -y torch torchvision

# Step 3/5: Installing PyTorch and TorchVision for ROCm5.6
echo ""
echo "Step 3/5: Installing PyTorch and TorchVision for ROCm5.6"
echo ""
pip install --pre torch torchvision --index-url https://download.pytorch.org/whl/nightly/rocm5.6

# Step 3/5: Removing Unwanted bitsandbytes Installation
echo ""
echo "Step 3/5: Removing Unwanted bitsandbytes Installation"
echo ""
pip uninstall bitsandbytes

# Step 4/5: Cloning the bitsandbytes-rocm Fork's Repo
echo ""
echo "Step 4/5: Cloning the bitsandbytes-rocm Fork's Repo"
echo ""
cd venv
git clone https://github.com/arlo-phoenix/bitsandbytes-rocm-5.6.git bitsandbytes
cd bitsandbytes

# Step 5/5: Installing bitsandbytes-rocm
echo ""
echo "Step 5/5: Installing bitsandbytes-rocm"
echo ""
export ROCM_HOME=/opt/rocm-5.6.0
make hip ROCM_TARGET=gfx1100
pip install .
pip install scipy  # I think there's a requirements problem somewhere (at least in kohya_ss)
cd ..
cd ..
