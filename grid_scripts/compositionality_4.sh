#!/bin/bash

CONFIG_DIR="configs/gr_transformer/compos_4/" 
for config in $(ls ${CONFIG_DIR})
do
    config_name="$(basename -s .yaml $config)"
    output_dir="/srv/local2/estengel/models/compositional_4/${config_name}"  
    mkdir -p ${output_dir} 
    export CHECKPOINT_DIR=$output_dir
    export CONFIG=${CONFIG_DIR}/${config} 
    echo "CHECKPOINT_DIR = ${CHECKPOINT_DIR}"
    echo "CONFIG = ${CONFIG}" 
    ./grid_scripts/train_gr.sh
    ./grid_scripts/test_gr.sh
done
