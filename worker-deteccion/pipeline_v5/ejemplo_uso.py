import pipeline_v5.utils as utils
import torch

# 1.- Definir directorio de pesos
weights_path = "pipeline_v5/rep2_weights.pt"
device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
model = utils.load_model(weights_path, device)

OOD_path = "pipeline_v5/OOD_detector.pkl"
OOD_detector = utils.load_OOD_detector(OOD_path)
# Arreglo 1D con información de la traza
input_signal = 0

# Lista de tuplas [[indice_inicio1, indice_fin1],[indice_inicio2, indice_fin2],...] para cada evento presente en la traza
ev = [[0, len(input_signal)]]

# 2.- Clasificacion: "VT": 1.0, "LP": 2.0, "TR": 3.0, "OT": 4.0
tpclass = utils.predict(input_signal, ev, model, OOD_detector)
