from pydantic import BaseModel, Field
from typing import Optional
import time
import uuid


class TraceInput(BaseModel):
    """
    Payload que llega al endpoint POST /ingesta/traza.
    Representa una traza sísmica detectada por una estación.
    """

    volcan_id: str = Field(
        ..., min_length=2, max_length=5, description="Código volcán (ej: VLL)"
    )
    estacion_id: str = Field(
        ..., min_length=2, max_length=5, description="Código estación (ej: PFT)"
    )
    componente: str = Field(
        "Z", max_length=1, description="Componente sísmica: Z, N, E"
    )
    inicio_unix: float = Field(
        default_factory=time.time, description="Timestamp inicio traza (unix)"
    )
    duracion_seg: float = Field(
        30.0, gt=0, description="Duración de la traza en segundos"
    )
    muestra_hz: int = Field(100, gt=0, description="Frecuencia de muestreo")
    extra: Optional[dict] = Field(default_factory=dict)

    def generar_evento_id(self) -> str:
        """Genera ID único tipo: VLL-20260303-a1b2"""
        from datetime import datetime, timezone

        dt = datetime.fromtimestamp(self.inicio_unix, tz=timezone.utc)
        sufijo = uuid.uuid4().hex[:6]
        return f"{self.volcan_id}-{dt.strftime('%Y%m%d')}-{sufijo}"


class EventoResponse(BaseModel):
    evento_id: str
    estado: str
    mensaje: str = ""
