"""
WWSClient — Cliente del servidor WWS (Winston Wave Server).

Interfaz diseñada para ser idéntica a la producción real. Cuando el servidor
WWS de Geología esté disponible, solo se reemplaza el bloque marcado con
"── IMPLEMENTACIÓN DUMMY ──" por la llamada real a WavePyWWS, sin cambiar
la firma ni el contrato de la función.

Uso real esperado (producción):
    xtime, data = WavePyWWS.getWavefromWWS(date2, date3, station, channel,
                                             volcano, WWShost, WWSport)
    # guardar data como MiniSEED usando ObsPy

Uso actual (dummy / POC):
    Copia un archivo template de /templates/ al volumen compartido /shared/data/
    renombrándolo con el timestamp de la ventana solicitada.
"""

import logging
import shutil
from datetime import datetime
from pathlib import Path

log = logging.getLogger("wws.client")

# Indica si el cliente está en modo dummy (sin servidor real)
DUMMY_MODE = True


class WWSClient:
    """
    Cliente abstracto del servidor WWS.

    En producción: inicializar con host/port reales y la implementación
    interna de get_waveform() hará la llamada de red real.

    En dummy: copia archivos template del directorio templates_dir.
    """

    def __init__(self, host: str, port: int, templates_dir: Path):
        self.host = host
        self.port = port
        self.templates_dir = Path(templates_dir)
        if DUMMY_MODE:
            log.warning(
                "WWSClient en modo DUMMY (sin conexión real). "
                "host=%s port=%d templates=%s",
                host, port, self.templates_dir,
            )
        else:
            log.info("WWSClient conectado a %s:%d", host, port)

    def get_waveform(
        self,
        estacion: str,
        componente: str,
        canal: str,
        inicio: datetime,
        fin: datetime,
        output_dir: Path,
    ) -> str:
        """
        Descarga una traza sísmica del servidor WWS y la guarda en output_dir.

        Args:
            estacion:   Código de estación (ej: "FU2", "CHS")
            componente: Componente sísmica (ej: "Z")
            canal:      Canal completo (ej: "HHZ")
            inicio:     Inicio de la ventana temporal
            fin:        Fin de la ventana temporal
            output_dir: Directorio donde guardar el archivo .mseed

        Returns:
            str: Nombre del archivo .mseed guardado en output_dir.

        Raises:
            FileNotFoundError: Si no hay template para la estación (modo dummy).
            ConnectionError:   Si no se puede conectar al servidor WWS (producción).
        """
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        if DUMMY_MODE:
            return self._get_waveform_dummy(estacion, componente, inicio, fin, output_dir)
        else:
            # ── IMPLEMENTACIÓN REAL (sustituir este bloque en producción) ──────
            # import WavePyWWS
            # station = estacion + componente
            # xtime, data = WavePyWWS.getWavefromWWS(
            #     inicio.strftime("%Y-%m-%d %H:%M:%S"),
            #     fin.strftime("%Y-%m-%d %H:%M:%S"),
            #     station, canal, "99", self.host, self.port
            # )
            # filename = f"{estacion}_{componente}_{inicio.strftime('%Y%m%d_%H%M%S')}.mseed"
            # dest = output_dir / filename
            # <guardar data como MiniSEED con ObsPy>
            # return filename
            # ───────────────────────────────────────────────────────────────────
            raise NotImplementedError("Modo producción no implementado aún.")

    # ── DUMMY ────────────────────────────────────────────────────────────────

    def _get_waveform_dummy(
        self,
        estacion: str,
        componente: str,
        inicio: datetime,
        fin: datetime,
        output_dir: Path,
    ) -> str:
        """
        Simula la descarga copiando un archivo template y renombrándolo
        con el timestamp de la ventana solicitada.
        """
        # Buscar template que coincida con la estación (ej: TC.FU2..HH.mseed)
        candidatos = list(self.templates_dir.glob(f"*{estacion}*"))
        if not candidatos:
            raise FileNotFoundError(
                f"No hay archivo template para estación '{estacion}' "
                f"en {self.templates_dir}. "
                f"Archivos disponibles: {[f.name for f in self.templates_dir.glob('*.mseed')]}"
            )

        template = candidatos[0]
        ts       = inicio.strftime("%Y%m%d_%H%M%S")
        filename = f"{estacion}_{componente}_{ts}.mseed"
        dest     = output_dir / filename

        shutil.copy2(str(template), str(dest))
        log.info(
            "DUMMY: %s → %s (ventana %s → %s)",
            template.name, filename,
            inicio.strftime("%Y-%m-%d %H:%M:%S"),
            fin.strftime("%Y-%m-%d %H:%M:%S"),
        )
        return filename

    def ping(self) -> bool:
        """
        Verifica conectividad con el servidor WWS.
        En dummy, siempre retorna True.
        """
        if DUMMY_MODE:
            return True
        try:
            # En producción: intentar handshake con el servidor
            # import socket
            # sock = socket.create_connection((self.host, self.port), timeout=5)
            # sock.close()
            return True
        except Exception as e:
            log.warning("WWS ping fallido (%s:%d): %s", self.host, self.port, e)
            return False
