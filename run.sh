export NCCL_TIMEOUT=1800

# ACCELERATE_CONFIG="accelerate_config/muti-gpu.yaml"
ACCELERATE_CONFIG="accelerate_config/single_gpu.yaml"
CONFIG="config/examples/train_lora_flux_kontext_test_2.yaml"

accelerate launch --config_file $ACCELERATE_CONFIG run.py $CONFIG