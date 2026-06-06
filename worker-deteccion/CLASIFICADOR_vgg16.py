import numpy as np
from PIL import Image
from matplotlib import cm
from scipy import signal
import torch
from torch import nn
import torchvision.transforms as transforms
from torchvision.models import vgg16_bn
import matplotlib.pyplot as plt


def preprocesamiento(signal_data, spect_size=(150, 150)):
    """Aplicación de filtro pasa-bandas y cálculo de espectrograma"""
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
    Sxx = Sxx[0:300, :]
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


def input_gen(signal_data):
    img_RGB, filtered_signal = preprocesamiento(signal_data, spect_size=(150, 150))
    img_RGB = np.flip(img_RGB, axis=0)
    fig = plt.figure(figsize=(15, 7.5), dpi=10)
    plt.plot(filtered_signal, color="black", lw=7)
    plt.xlim(0, 12000)  # plotea señal hasta los 120s (2 minutos)
    plt.axis("off")
    plt.tight_layout()
    fig.canvas.draw()
    sígnal_plot = np.array(fig.canvas.renderer.buffer_rgba())[:, :, :3]
    plt.close()
    image = np.concatenate([img_RGB, sígnal_plot], axis=0)
    # plt.imshow(image)
    # plt.axis("off")
    # plt.show()
    return image


def cargar_modelo(weights_path, n_classes=4):
    """Cargar modelo pre-entrenado"""
    model = vgg16_bn(weights=None)
    device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
    # print("using", device)
    # Cambiar salidas
    model.classifier[6] = nn.Linear(in_features=4096, out_features=n_classes, bias=True)
    # Agregar Capas de drop-out entre cada capa convolucional
    feats_list = list(model.features)
    new_feats_list = []
    for feat in feats_list:
        new_feats_list.append(feat)
        if isinstance(feat, nn.Conv2d):
            new_feats_list.append(nn.Dropout(p=0.3, inplace=False))
    model.features = nn.Sequential(*new_feats_list)
    # cargar modelo
    checkpoint = torch.load(weights_path, map_location=torch.device("cpu"))
    model.load_state_dict(checkpoint["model_state_dict"])
    # enviar modelo a gpu o cpu
    return model.to(device)


def clasificar(signal_data, ev, model, batch_size=None):
    """Preparar modelo para predicción (sin cálculo de gradientes)"""
    model.eval()
    device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
    transform_ = transforms.Compose(
        [transforms.ToTensor(), transforms.Normalize((0.5, 0.5, 0.5), (0.5, 0.5, 0.5))]
    )
    vector_salida = np.zeros(len(ev))
    if batch_size is None:
        for idx, event_idx in enumerate(ev):
            print(f"clasificando evento {idx+1}/{len(ev)}")
            evento = signal_data[event_idx[0] : event_idx[1]]
            try:
                img_RGB = input_gen(evento)
                img_RGB = transform_(img_RGB).to(device)
                img_RGB = torch.unsqueeze(img_RGB, 0)
                # Obtener salida de acuerdo a varias pasadas
                with torch.no_grad():
                    output = model(img_RGB)
                # clase predicha
                clase_predicha = output.argmax(axis=1).item()
                # if clase_predicha > 3.0:
                # clase_predicha = 3.0
                clases = {0: "VT", 1: "LP", 2: "TR", 3: "OT"}
                clases_OVDAS = {"VT": 1.0, "LP": 2.0, "TR": 3.0, "OT": 4.0}
                # guardar en vector de salida
                vector_salida[idx] = clases_OVDAS[clases[clase_predicha]]
            except ValueError:
                print(
                    f"Evento n°{idx} (index: {event_idx[0]} - {event_idx[1]}) muy corto, se asignará etiqueta 4 : 'OT"
                )
                vector_salida[idx] = 4.0
    # if batch_size is not None:
    return vector_salida
