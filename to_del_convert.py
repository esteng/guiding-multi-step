import json
import pickle as pkl
import pathlib

#paths = ["/srv/local1/estengel/blocks_data_for_release/good_robot_sim/stacks/", 
#         "/srv/local1/estengel/blocks_data_for_release/good_robot_sim/rows/", 
#         "/srv/local1/estengel/blocks_data_for_release/good_robot_real/rows/1/", 
#         "/srv/local1/estengel/blocks_data_for_release/good_robot_real/stacks/1/", 
#         "/srv/local1/estengel/blocks_data_for_release/good_robot_real/stacks/2/"]
paths = ["/srv/local1/estengel/blocks_data_for_release/good_robot_real/rows/1/", 
         "/srv/local1/estengel/blocks_data_for_release/good_robot_real/stacks/1/", 
         "/srv/local1/estengel/blocks_data_for_release/good_robot_real/stacks/2/"]


for p in paths: 
    p = pathlib.Path(p) 
    print(p) 
    read_path = p.joinpath("with_actions.pkl") 
    data = pkl.load(open(read_path, 'rb')) 

    lines = [pair.to_jsonline() for pair in data]
    out_path = p.joinpath("pairs.jsonlines")
    with open(out_path, "w") as f1:
        for line in lines:
            f1.write(line.strip() + "\n") 
    print(f"wrote to {out_path}")
