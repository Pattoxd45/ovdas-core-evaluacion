import numpy as np
from PIL import Image
from matplotlib import cm
from scipy import signal
import torch
from torch import nn
import torchvision.transforms as transforms
from torchvision.models import vgg16_bn
import matplotlib.pyplot as plt
import pickle
from cleanlab.outlier import OutOfDistribution


def preprocessing(signal_data: np.ndarray, spect_size: tuple = (112, 112)):
    """
    Aplicación de filtro pasa-bandas y cálculo de espectrograma

    Args:
        signal_data (np.ndarray): La traza de entrada cruda como NumPy array.
        spect_size (tuple[int, int]): Tamaño del espectrograma como tupla.

    Returns:
        np.ndarray: Espectrograma ajustado al tamaño definido por spect_size.
        np.ndarray: Señal filtrada.

    Ejemplo:
        img_RGB, filtered_signal = preprocessing(signal_data, (150,150))
    """
    nperseg_ = 100
    if nperseg_ >= len(signal_data):
        raise ValueError(f"Señal muy corta, debe contener al menos {nperseg_} muestras")
    filter_but = signal.butter(
        N=5, Wn=[1.0, 15], btype="bandpass", fs=100, output="sos"
    )
    filtered_signal = signal.sosfiltfilt(filter_but, signal_data)
    f, t, Sxx = signal.spectrogram(
        filtered_signal,
        fs=100,
        window="hamming",
        nperseg=nperseg_,
        noverlap=90,
        nfft=1024,
        mode="complex",
    )
    Sxx = Sxx[0:200, :]  # mostrar hasta 20 Hz
    product = Sxx * np.conj(Sxx)
    temp = np.log10(100 * product.real + 1e-5)
    mini = temp.min()
    maxi = temp.max()
    temp = (temp - mini) / (maxi - mini)
    k = temp.min() + 0.5 * (temp.max() - temp.min())
    temp[temp <= k] = k
    temp = (temp - temp.min()) / (temp.max() - temp.min())
    im = Image.fromarray(np.uint8(temp * 255))
    im = im.resize((spect_size))
    pix = np.array(im)
    img_RGB = getattr(cm, "jet")(pix, bytes=True)[:, :, :3]  # RGB
    return img_RGB, filtered_signal


def obtain_rep(trace_data: np.ndarray):
    """
    Se obtiene la representación 2 (spectrogram + waveform) a partir de una señal cruda

    Args:
        signal_data (np.ndarray): Traza como NumPy array.

    Returns:
        np.ndarray: Representación de la señal RGB (alto x ancho x 3)

    Ejemplo:
        img_RGB = obtain_rep(signal_data)
    """
    # filtrado y obtención de espectrograma
    img_RGB, filtered_signal = preprocessing(trace_data, spect_size=(150, 150))
    img_RGB = np.flip(img_RGB, axis=0)
    # Selección de reresentación
    fig = plt.figure(figsize=(15, 7.5), dpi=10)
    plt.plot(filtered_signal, color="black", lw=7)
    plt.xlim(0, 12000)  # dibuja señal hasta los 120s (2 minutos)
    plt.axis("off")
    plt.tight_layout()
    fig.canvas.draw()
    sígnal_plot = np.array(fig.canvas.renderer.buffer_rgba())[:, :, :3]
    plt.close()
    image = np.concatenate([img_RGB, sígnal_plot], axis=0)
    return image


def load_model(weights_path: str, device: str = "cpu"):
    """
    Cargar pesos a modelo

    Args:
        weights_path (str): String con dirección a los pesos (.pt)
        device (str): 'cpu' o 'cuda'.

    Returns:
        nn.Module: Modelo con pesos cargado al dispositivo correspondiente.

    Ejemplo:
        weights_path = "pipeline/Pesos/rep2_3clases_weights.pt"
        device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
        model = load_model(weights_path, 3, device)

    """
    model = vgg16_bn(weights=None)
    print("using", device)
    # Cambiar salidas
    model.classifier[6] = nn.Linear(in_features=4096, out_features=3, bias=True)
    # Agregar Capas de drop-out entre cada capa convolucional
    feats_list = list(model.features)
    new_feats_list = []
    for feat in feats_list:
        new_feats_list.append(feat)
        if isinstance(feat, nn.Conv2d):
            new_feats_list.append(nn.Dropout(p=0.3, inplace=False))
    model.features = nn.Sequential(*new_feats_list)
    model.classifier.add_module("7", nn.Softmax(dim=1))
    # cargar modelo
    checkpoint = torch.load(weights_path, map_location=torch.device("cpu"))
    model.load_state_dict(checkpoint["model_state_dict"])
    # enviar modelo a gpu o cpu
    return model.to(device)


def load_OOD_detector(OOD_path: str, device: str = "cpu"):
    """
    Args:
        weights_path (str): String con dirección a los pesos (.pt)
        device (str): 'cpu' o 'cuda'.
    """
    with open(OOD_path, "rb") as f:
        OOD_detector = pickle.load(f)
    return OOD_detector


def predict(
    signal_data: np.ndarray,
    ev: list,
    model: nn.Module,
    OOD_detector: OutOfDistribution,
    OOD_threshold: np.float32 = 0.274,
    return_probs: bool = False,
):
    """
    Clasifica traza separada por índices

    Args:
        signal_data (np.ndarray): Traza cruda con multiples eventos.
        ev (list): Índices (tupla) de eventos detectados en la traza.
        model (nn.Module): Modelo cargado.
        OOD_detector: Detector OOD (hay que cargarlo antes con load_OOD_detector())
        OOD_threshold: Umbral para detección OOD (más bajo detecta menos, más alto confunde más ID con OOD)
        return_probs: Si True, retorna también la matriz de probabilidades (n_eventos, 4)
                      en el orden [prob_lp, prob_tr, prob_vt, prob_ot], que coincide
                      con las columnas del INSERT SQL de identificacion_senal.

    Returns:
        np.ndarray: arreglo 1D con clases predichas por evento (1: VT, 2: LP, 3: TR, 4: OT).
        np.ndarray (solo si return_probs=True): matriz (n_eventos, 4) con probabilidades
            [prob_lp, prob_tr, prob_vt, prob_ot]. Para eventos OOD se asigna prob_ot=1.0
            y el resto en cero.

    Ejemplo:
        tpclass = predict(input_signal, ev, model)
        tpclass, probs = predict(input_signal, ev, model, return_probs=True)
    """
    model.eval()
    device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
    transform_ = transforms.Compose(
        [transforms.ToTensor(), transforms.Normalize((0.5, 0.5, 0.5), (0.5, 0.5, 0.5))]
    )
    clases = {0: "VT", 1: "LP", 2: "TR", 3: "OT"}
    clases_OVDAS = {"VT": 1.0, "LP": 2.0, "TR": 3.0, "OT": 4.0}

    vector_salida = np.zeros(len(ev))
    # Columnas: [prob_lp, prob_tr, prob_vt, prob_ot]
    # Orden idéntico al INSERT SQL: prob_lp, prob_tr, prob_vt, prob_ot
    probs_salida = np.zeros((len(ev), 4))

    for idx, event_idx in enumerate(ev):
        print(f"clasificando evento {idx+1}/{len(ev)}")
        evento = signal_data[event_idx[0] : event_idx[1]]
        try:
            img_RGB = obtain_rep(evento)
            img_RGB = transform_(img_RGB).to(device)
            img_RGB = torch.unsqueeze(img_RGB, 0)
            with torch.no_grad():
                feats = model.features(img_RGB)
                flatten_feats = feats.view(feats.shape[0], -1).detach().cpu()
                KNN_scores = OOD_detector.score(features=flatten_feats)
                # Siempre se calcula la salida del modelo para obtener las probabilidades
                output = model(img_RGB)
                softmax_probs = output.detach().cpu().numpy()[0]  # [vt, lp, tr]
                if KNN_scores >= OOD_threshold:
                    clase_predicha = output.argmax(axis=1).item()
                    # Modelo: índice 0=VT, 1=LP, 2=TR → reordenar a [lp, tr, vt, ot]
                    probs_salida[idx] = [
                        float(softmax_probs[1]),  # prob_lp
                        float(softmax_probs[2]),  # prob_tr
                        float(softmax_probs[0]),  # prob_vt
                        0.0,                       # prob_ot (no OOD)
                    ]
                else:
                    clase_predicha = 3  # OT por detección OOD
                    probs_salida[idx] = [0.0, 0.0, 0.0, 1.0]

            vector_salida[idx] = clases_OVDAS[clases[clase_predicha]]
        except ValueError:
            print(
                f"Evento n°{idx} (index: {event_idx[0]} - {event_idx[1]}) muy corto, se asignará etiqueta 4 : 'OT"
            )
            vector_salida[idx] = 4.0
            probs_salida[idx] = [0.0, 0.0, 0.0, 1.0]  # OT por señal inválida

    if return_probs:
        return vector_salida, probs_salida
    return vector_salida
