## Setting up the RaspberryPI
Install required building packages (Linux):
```bash
sudo apt update
sudo apt install -y cmake \
    libxrandr-dev \
    libasound2-dev \
    libjack-jackd2-dev \
    ladspa-sdk \
    libcurl4-openssl-dev \
    libfreetype6-dev \
    libx11-dev \
    libxcomposite-dev \
    libxcursor-dev \
    libxext-dev \
    libxinerama-dev \
    libxrender-dev \
    libwebkit2gtk-4.0-dev \
    libglu1-mesa-dev mesa-common-dev \
    build-essential \
    git \
    libjpeg-dev \
    libpng-dev \
    libeigen3-dev \
    libssl-dev \
    python3-dev \
    python3-pip \
    zip unzip
```
Get the VST3Sourcecode (or copy it from this GIT):  
```bash
curl -LO https://groovetransformer.github.io/assets/zip/VST3SourceCode.zip
unzip VST3SourceCode.zip -d ./VST3SourceCode
unzip -o Source.zip -d ./VST3SourceCode
```
Building set up:
```bash
cd VST3SourceCode
mkdir build
cd build
cmake ..
```
Since the app doesnt package the right type of PyTorch update the PyTorch version to the correct version. This new version will be build using Python:
```
cd ~/Library
mv libtorch-2.0.0-/ ./libtorch-2.0.0-old
git clone --recursive https://github.com/pytorch/pytorch

python3 -m venv venv
. ./venv/bin/activate
pip3 install --upgrade setuptools wheel
git checkout v2.1.2
git submodule sync
git submodule update --init --recursive
pip install pyyaml
python3 setup.py install --prefix=/home/admin/Library/libtorch-2.0.0-

cd ~/VST3SourceCode/build
make
```

## Start running

### Start the wrapper:
```python main.py --host-ip [IP] --host-port [PORT] --listen-port [PORT]```

### Start the plugin (Have patience, the UI is slow):
```./start_plugin.sh```

If there is no MIDI being received or send press options -> Audio/MIDI Settings.
Select the input and output ports that include GrooveWrap, if not visible start the Python code, close the Audio/MIDI Settings window and reopen.

Press play in the GUI.