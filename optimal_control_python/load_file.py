import time
import pickle

from BiorbdViz import BiorbdViz

file_path = "results/simulate.bob"

if not isinstance(file_path, str):
    t = time.localtime(time.time())
    file_path = f"results/{t.tm_year}_{t.tm_mon}_{t.tm_mday}_upDown_interpolate.bob"

with open(file_path, "rb") as file:
    data = pickle.load(file)

data_interpolate, _ = data["data"]

b = BiorbdViz("../models/BrasViolon.bioMod")
b.load_movement(data_interpolate["q"].T)
b.exec()
