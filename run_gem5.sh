if [[ "$1" == "-h" || "$1" == "--help" ]]; then
  cat <<'EOF'
Usage: run_gem5.sh --gem5-ckp-dir DIR --experiment NAME --snapshot NAME --inst N --cores N [--cpu MODEL] [--yaml FILE] [--qcow2 FILE] [--debug-flags FLAGS] [--branch-trace]

Arguments (required):
  --gem5-ckp-dir  Checkpoint root directory (the run/ folder of the qflex
                  experiment, which holds <snapshot>.gem/)
  --experiment    Experiment name
  --snapshot      Snapshot name
  --inst          Instruction count
  --cores         Number of cores (must match the device tree)

Optional:
  --cpu           CPU model: atomic (default), minor, hpi, o3
  --yaml          QEMU device tree as YAML (default: ../dtb/virt.yaml)
  --qcow2         QEMU image holding the snapshot, used once to extract the
                  disk (default: <gem5-ckp-dir>/root.qcow2)
  --debug-flags   gem5 debug flags (default: All)

The disk:
  gem5 needs the guest's disk as it was at snapshot time. The first run
  extracts it from the qcow2 snapshot into <snapshot>.gem/disk.img (raw,
  sparse; never modified afterwards). Every run then simulates on its own
  sparse copy in the output directory, because gem5 writes to the disk.

Example:
  run_gem5.sh --gem5-ckp-dir /checkpoints --experiment OoO --snapshot snapshot_0 --inst 100000 --cores 1 --branch-trace
EOF
  exit 0
fi

export M5_PATH=$(pwd)/bin/m5
GEM5_HOME=$(pwd)/gem5
GEM5_CFG=$GEM5_HOME/configs/example/arm/qflex_restore.py

GEM5_CKP_DIR=""
EXPERIMENT=""
SNAPSHOT=""
INST=""
CORES=""
BRANCH_TRACE=""
CPU="atomic"
YAML="$(pwd)/../dtb/virt.yaml"
QCOW2=""
DEBUG_FLAGS="All"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --gem5-ckp-dir)
      GEM5_CKP_DIR="$2"
      shift 2
      ;;
    --experiment)
      EXPERIMENT="$2"
      shift 2
      ;;
    --snapshot)
      SNAPSHOT="$2"
      shift 2
      ;;
    --inst)
      INST="$2"
      shift 2
      ;;
    --cores)
      CORES="$2"
      shift 2
      ;;
    --cpu)
      CPU="$2"
      shift 2
      ;;
    --yaml)
      YAML="$2"
      shift 2
      ;;
    --qcow2)
      QCOW2="$2"
      shift 2
      ;;
    --debug-flags)
      DEBUG_FLAGS="$2"
      shift 2
      ;;
    --branch-trace)
      BRANCH_TRACE="--branch-trace"
      shift 1
      ;;
    *)
      echo "Unknown argument: $1"
      exit 1
      ;;
  esac
done

if [[ -z "$GEM5_CKP_DIR" || -z "$EXPERIMENT" || -z "$SNAPSHOT" || -z "$INST" || -z "$CORES" ]]; then
  echo "Missing required arguments."
  exit 1
fi

CKPT_DIR="${GEM5_CKP_DIR}/${SNAPSHOT}.gem"
QCOW2="${QCOW2:-${GEM5_CKP_DIR}/root.qcow2}"

OUTDIR=sim_outs/${EXPERIMENT}/${SNAPSHOT}
mkdir -p $OUTDIR
touch ${OUTDIR}

# The boot flash. m5.cpt has two memory stores: store1 is the RAM (dumped
# by parallel-qemu) and store0 the boot flash at address 0. QEMU's flash
# devices have no drive behind them, so the guest state there is empty:
# store0 is a file of zeros, of the size m5.cpt gives it (gem5 stops if the
# file is missing). truncate makes it sparse: it takes no space.
STORE0="${CKPT_DIR}/system.physmem.store0.pmem"
if [[ ! -f "$STORE0" ]]; then
  STORE0_SIZE=$(awk '/^\[system.physmem.store0\]/ {s=1; next}
                     /^\[/ {s=0}
                     s && /^range_size=/ {sub("range_size=", ""); print; exit}' \
                "${CKPT_DIR}/m5.cpt")
  if [[ -z "$STORE0_SIZE" ]]; then
    echo "ERROR: no [system.physmem.store0] range_size in ${CKPT_DIR}/m5.cpt"
    exit 1
  fi
  truncate -s "$STORE0_SIZE" "$STORE0" || exit 1
fi

# The disk. The checkpoint holds the state of the disk device (m5.cpt,
# converted from virtio-info.json) but not its content: without the
# content, the guest's first disk access would read zeros.
#
# 1. Once per snapshot: extract the disk of the snapshot from the qcow2
#    (qemu-img -l snapshot.name) as a raw image, the only format gem5's
#    RawDiskImage reads. -U: read even if a QEMU has the image open. Written
#    to a temporary name first, so an interrupted extraction is not reused.
DISK_REF="${CKPT_DIR}/disk.img"
if [[ ! -f "$DISK_REF" ]]; then
  QEMU_IMG=$(command -v qemu-img || echo "${GEM5_CKP_DIR}/qemu-img")
  if [[ ! -f "$QCOW2" || ! -x "$QEMU_IMG" ]]; then
    echo "ERROR: ${DISK_REF} not found, and cannot extract it: need the qcow2"
    echo "       (${QCOW2}, see --qcow2) and qemu-img (${QEMU_IMG})."
    exit 1
  fi
  echo "Extracting the disk of snapshot ${SNAPSHOT} from ${QCOW2}..."
  "$QEMU_IMG" convert -U -l snapshot.name="${SNAPSHOT}" -O raw \
      "$QCOW2" "${DISK_REF}.tmp" || { rm -f "${DISK_REF}.tmp"; exit 1; }
  mv "${DISK_REF}.tmp" "$DISK_REF"
fi
# 2. Every run: gem5 writes to the disk, so it gets its own copy and
#    disk.img stays the snapshot's disk. --sparse=always: the empty parts
#    of the (mostly empty) disk take no space.
RUN_DISK="${OUTDIR}/disk.img"
cp --sparse=always "$DISK_REF" "$RUN_DISK" || exit 1
DISK_ARG="--disk-image=${RUN_DISK}"

$GEM5_HOME/build/ARM/gem5.opt --debug-flags=${DEBUG_FLAGS} --outdir=${OUTDIR} --debug-file=debug.insts  $GEM5_CFG --yaml "${YAML}" -I $INST ${DISK_ARG} --cpu ${CPU} --restore "${CKPT_DIR}" --num-cores ${CORES} --mem-size 8192MiB --mem-channels=1 ${BRANCH_TRACE}