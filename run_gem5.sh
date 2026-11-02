if [[ "$1" == "-h" || "$1" == "--help" ]]; then
  cat <<'EOF'
Usage: run_gem5.sh --gem5-ckp-dir DIR --experiment NAME --snapshot NAME --inst N --cores N [--cpu MODEL] [--yaml FILE] [--branch-trace]

Arguments (required):
  --gem5-ckp-dir  Checkpoint root directory
  --experiment    Experiment name
  --snapshot      Snapshot name
  --inst          Instruction count
  --cores         Number of cores (must match the device tree)

Optional:
  --cpu           CPU model: atomic (default), minor, hpi, o3
  --yaml          QEMU device tree as YAML (default: ../dtb/virt.yaml)

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

OUTDIR=sim_outs/${EXPERIMENT}/${SNAPSHOT}
mkdir -p $OUTDIR
touch ${OUTDIR}

# The disk image is optional: QEMU's dump does not contain one yet.
DISK_ARG=""
if [[ -f "${CKPT_DIR}/${SNAPSHOT}.img" ]]; then
  DISK_ARG="--disk-image=${CKPT_DIR}/${SNAPSHOT}.img"
fi

$GEM5_HOME/build/ARM/gem5.opt --debug-flags=All --outdir=${OUTDIR} --debug-file=debug.insts  $GEM5_CFG --yaml "${YAML}" -I $INST ${DISK_ARG} --cpu ${CPU} --restore "${CKPT_DIR}" --num-cores ${CORES} --mem-size 8192MiB --mem-channels=1 ${BRANCH_TRACE}
