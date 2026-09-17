#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${ROOT_DIR}/.." && pwd)"

# TODO: these packages must be installed in the Dockerfile, not here
if command -v apt-get >/dev/null 2>&1; then
  if command -v sudo >/dev/null 2>&1; then
    SUDO="sudo"
  else
    SUDO=""
  fi
  $SUDO apt-get update
  $SUDO apt-get remove -y scons
  $SUDO apt-get install -y \
    build-essential \
    bzip2 \
    git \
    libboost-all-dev \
    libprotobuf-dev \
    libsqlite3-dev \
    m4 \
    netcat-openbsd \
    pkg-config \
    sshpass \
    protobuf-compiler \
    gdb-multiarch \
    python3 \
    python3-dev \
    python3-pip \
    python3-jinja2 \
    python3-six \
    qemu-utils \
    vim \
    wget \
    zlib1g-dev \
    nlohmann-json3-dev
  python3 -m pip install "scons==3.1.2"
  python3 -m pip install --break-system-packages gdown
fi

cd "$ROOT_DIR"

echo initializing gem5 submodule
if [[ ! -e gem5 ]]; then
  git submodule update --init --recursive --remote gem5
elif [[ -f gem5/.git ]]; then
  git submodule update --init --recursive --remote gem5
else
  echo "gem5 exists and is not a submodule; skipping submodule init" >&2
fi

echo build gem5
cd gem5
scons -j32 build/ARM/gem5.opt CXX=g++ CXXFLAGS="-std=c++17"
cd ..

echo Getting ARM kernel image files for gem5
cd bin/m5
wget http://dist.gem5.org/dist/v22-0/arm/aarch-system-20220707.tar.bz2
tar -xvf aarch-system-20220707.tar.bz2

cd "$PROJECT_ROOT"

echo Setup is complete!
