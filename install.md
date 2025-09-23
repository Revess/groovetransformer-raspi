curl -LO https://groovetransformer.github.io/assets/zip/VST3SourceCode.zip
sudo apt install cmake libxrandr-dev libasound2-dev libjack-jackd2-dev ladspa-sdk libcurl4-openssl-dev libfreetype6-dev libx11-dev libxcomposite-dev libxcursor-dev libxext-dev libxinerama-dev libxrender-dev libwebkit2gtk-4.0-dev libglu1-mesa-dev mesa-common-dev
mkdir build
cd build
cmake ..
cd ~/Library
mv libtorch-2.0.0-/ ./libtorch-2.0.0-old
git clone --recursive https://github.com/pytorch/pytorch
sudo apt update
sudo apt install -y build-essential cmake git libjpeg-dev libpng-dev
sudo apt install -y libeigen3-dev libssl-dev
sudo apt install -y python3-dev python3-pip
python3 -m venv venv
. ./venv/bin/activate
pip3 install --upgrade setuptools wheel
git checkout v2.1.2
git submodule sync
git submodule update --init --recursive
pip install pyyaml
python3 setup.py install --prefix=/home/admin/Library/libtorch-2.0.0

