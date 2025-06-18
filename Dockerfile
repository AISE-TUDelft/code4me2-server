# using the official python image
FROM nvidia/cuda:12.6.0-runtime-ubuntu22.04

# as the official image is based on ubuntu, we need to update the package list
# and install some dependencies
RUN apt-get update && apt-get install -y \
    wget \
    libnvidia-ml-dev \
    software-properties-common \
    nvidia-container-toolkit && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/* \

# install miniconda to manage the python environment
# Note:
# Why did I install miniconda?
# I could have used the python image to install the dependencies, but I wanted to use miniconda
# because I was having a lot of issues with the python image and the dependencies.
# I was getting a lot of errors when installing the dependencies, and I was not able to fix them.
# And there were a lot of unanswered stack overflow questions about the issue I was facing
# So, I decided to use miniconda to install the dependencies, and it worked.
# feel free to revert back to the python image if you want to and see if you can fix the issue.
# it can be found in the commit with hash dd3df754fa46587e65c30d5882d4660af73d948d
RUN wget https://repo.anaconda.com/miniconda/Miniconda3-latest-Linux-x86_64.sh -O /tmp/miniconda.sh && \
    bash /tmp/miniconda.sh -b -p /opt/conda && \
    rm /tmp/miniconda.sh && \
    /opt/conda/bin/conda clean -ya

# add conda to the path
ENV PATH=/opt/conda/bin:$PATH

# Create a Conda environment with Python 3.10
RUN conda create -y -n myenv python=3.10 && conda clean -a -y

# install hugggingfacehub cli for logging in to the huggingface hub
RUN /bin/bash -c "source activate myenv && conda install -y -c conda-forge huggingface_hub[cli] && conda clean -a -y"

# set the working directory
WORKDIR /app

# Activate the Conda environment and install the required Python packages
COPY requirements.txt .
RUN /bin/bash -c "source activate myenv && pip install --no-cache-dir -r requirements.txt"

# ensure that uvicorn is installed
RUN /bin/bash -c "source activate myenv && pip install uvicorn"

# Copy the application code
COPY . .