import asyncio
import logging
import os
import platform
import re
import shutil
import subprocess
from typing import AsyncGenerator, Callable, Dict, List, Optional
from config import SourceKind

logger = logging.getLogger("audio.source")


def resolve_ffmpeg_bin() -> str:
    """Busca el ejecutable de ffmpeg en rutas conocidas de WinGet o en el PATH."""
    local_app_data = os.getenv("LOCALAPPDATA", "")
    gyan_full = os.path.join(
        local_app_data,
        r"Microsoft\WinGet\Packages\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe\ffmpeg-9.0.2-full_build\bin\ffmpeg.exe"
    )
    if os.path.isfile(gyan_full):
        return gyan_full

    packages_dir = os.path.join(local_app_data, r"Microsoft\WinGet\Packages")
    if os.path.isdir(packages_dir):
        for root, _, files in os.walk(packages_dir):
            if "ffmpeg.exe" in files:
                return os.path.join(root, "ffmpeg.exe")

    return shutil.which("ffmpeg") or "ffmpeg"


def list_audio_devices() -> List[Dict[str, str]]:
    """
    Detecta y lista los dispositivos de entrada de audio disponibles en el sistema operativo.
    En Windows usa DirectShow con nombres limpios y amigables; en Linux/Mac devuelve interfaces estándar.
    """
    devices: List[Dict[str, str]] = []
    system = platform.system()

    if system == "Windows":
        ffmpeg_bin = resolve_ffmpeg_bin()
        try:
            res = shutil.which(ffmpeg_bin)
            if not res and not os.path.isfile(ffmpeg_bin):
                return devices

            # Ejecutar ffmpeg para listar dispositivos dshow
            proc = subprocess.run(
                [ffmpeg_bin, "-list_devices", "true", "-f", "dshow", "-i", "dummy"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="ignore"
            )

            lines = proc.stderr.splitlines()
            for i, line in enumerate(lines):
                if "(audio)" in line:
                    match_name = re.search(r'\"([^\"]+)\"\s+\(audio\)', line)
                    if match_name:
                        name = match_name.group(1)
                        alt_id = ""
                        # Si la siguiente línea contiene el Alternative name, capturarlo
                        if i + 1 < len(lines) and "Alternative name" in lines[i + 1]:
                            match_alt = re.search(r'Alternative name\s+\"([^\"]+)\"', lines[i + 1])
                            if match_alt:
                                alt_id = match_alt.group(1)
                        devices.append({
                            "id": name,
                            "name": name,
                            "alt_id": alt_id,
                            "kind": "dshow"
                        })
        except Exception as e:
            logger.warning("No se pudieron listar los dispositivos de audio DirectShow: %s", e)
    elif system == "Linux":
        devices.append({"id": "default", "name": "PulseAudio Default", "kind": "pulse"})
    elif system == "Darwin":
        devices.append({"id": ":0", "name": "Default Audio Device", "kind": "avfoundation"})

    return devices


class AudioSource:
    """
    Fábrica y supervisor de fuentes de audio para conferencias.
    Normaliza cualquier entrada (archivo, micrófono DirectShow o stream RTMP/HLS)
    a PCM s16le, 16.000 Hz, mono (3.200 B por chunk de 100 ms).
    Implementa cola acotada con backpressure y supervisor de reconexión.
    """

    SAMPLE_RATE = 16000
    CHANNELS = 1
    BYTES_PER_SAMPLE = 2  # s16le = 16 bits = 2 bytes
    CHUNK_DURATION_MS = 100
    # 16000 * 2 * 0.1 = 3200 bytes por chunk de 100ms
    CHUNK_SIZE_BYTES = int(SAMPLE_RATE * CHANNELS * BYTES_PER_SAMPLE * (CHUNK_DURATION_MS / 1000.0))
    MAX_QUEUE_CHUNKS = 50  # ~5 segundos de buffer en memoria para amortiguar jitter

    def __init__(
        self,
        kind: SourceKind,
        source_uri: str,
        loop: bool = False,
        on_degraded: Optional[Callable[[], None]] = None,
        on_recovered: Optional[Callable[[], None]] = None,
    ):
        self.kind = kind
        self.source_uri = source_uri
        self.loop = loop
        self.on_degraded = on_degraded
        self.on_recovered = on_recovered

        self._queue: asyncio.Queue[Optional[bytes]] = asyncio.Queue(maxsize=self.MAX_QUEUE_CHUNKS)
        self._process: Optional[subprocess.Popen] = None
        self._reader_task: Optional[asyncio.Task] = None
        self._stop_event = asyncio.Event()

    def _build_ffmpeg_cmd(self) -> List[str]:
        ffmpeg_bin = resolve_ffmpeg_bin()
        cmd = [ffmpeg_bin, "-hide_banner", "-loglevel", "error"]

        if self.kind == SourceKind.FILE:
            # Para archivos locales, -re mantiene el tiempo real del audio
            cmd.append("-re")
            if self.loop:
                cmd.extend(["-stream_loop", "-1"])
            cmd.extend(["-i", self.source_uri])

        elif self.kind == SourceKind.MIC:
            system = platform.system()
            if system == "Windows":
                uri = (self.source_uri or "").strip().strip('"\'')
                # Auto-sanear si se perdió la barra invertida en GUIDs @device_cm_... o @device_sw_...
                if "@device_" in uri and "\\" not in uri and "}" in uri:
                    uri = re.sub(r'}(wave_[A-Za-z0-9_\-]+|\{[A-Za-z0-9_\-]+\})', r'}\\\1', uri)
                input_dev = uri if uri.startswith("audio=") else f"audio={uri}"
                cmd.extend(["-f", "dshow", "-i", input_dev])
            elif system == "Darwin":
                cmd.extend(["-f", "avfoundation", "-i", self.source_uri or ":0"])
            else:
                cmd.extend(["-f", "pulse", "-i", self.source_uri or "default"])

        elif self.kind == SourceKind.STREAM:
            # Receptor RTMP en escucha para OBS o ingest HLS/URL
            if "rtmp://" in self.source_uri and ("0.0.0.0" in self.source_uri or ":1935" in self.source_uri):
                cmd.extend(["-f", "flv", "-listen", "1", "-i", self.source_uri])
            else:
                cmd.extend(["-i", self.source_uri])

        # Parámetros estándar comunes para entrega a Gemini Live ASR
        cmd.extend([
            "-f", "s16le",
            "-acodec", "pcm_s16le",
            "-ar", str(self.SAMPLE_RATE),
            "-ac", str(self.CHANNELS),
            "-",
        ])
        return cmd

    @staticmethod
    def _read_exact(stream, n: int) -> bytes:
        """Lee exactamente n bytes del stream síncrono."""
        buf = bytearray()
        while len(buf) < n:
            part = stream.read(n - len(buf))
            if not part:
                break
            buf.extend(part)
        return bytes(buf)

    async def _run_supervisor(self) -> None:
        """
        Ciclo de supervisión y lectura de chunks PCM con reconexión automática
        para micrófonos y streams en vivo.
        Usa subprocess.Popen con lectura en hilo executor para total compatibilidad
        con Windows SelectorEventLoop (habitual con uvicorn --reload).
        """
        backoff = 1.0
        max_backoff = 8.0
        loop = asyncio.get_running_loop()

        while not self._stop_event.is_set():
            cmd = self._build_ffmpeg_cmd()
            logger.info("Iniciando subproceso FFmpeg [%s]: %s", self.kind.value, " ".join(cmd))

            try:
                self._process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    bufsize=self.CHUNK_SIZE_BYTES * 4
                )
            except Exception as e:
                logger.error("No se pudo iniciar subproceso FFmpeg (%s): %s", type(e).__name__, e)
                if self.kind == SourceKind.FILE or self._stop_event.is_set():
                    break
                if self.on_degraded:
                    self.on_degraded()
                await asyncio.sleep(backoff)
                backoff = min(backoff * 1.5, max_backoff)
                continue

            assert self._process.stdout is not None
            consecutive_reads = 0

            try:
                while not self._stop_event.is_set():
                    chunk = await loop.run_in_executor(
                        None,
                        self._read_exact,
                        self._process.stdout,
                        self.CHUNK_SIZE_BYTES
                    )

                    if not chunk or len(chunk) < self.CHUNK_SIZE_BYTES:
                        if chunk:
                            await self._enqueue_chunk(chunk)
                        break

                    consecutive_reads += 1
                    if consecutive_reads == 10 and self.on_recovered:
                        self.on_recovered()
                        backoff = 1.0

                    await self._enqueue_chunk(chunk)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("Error leyendo flujo de FFmpeg [%s]: %s", self.kind.value, e)
            finally:
                if self._process and self._process.poll() is None:
                    try:
                        self._process.terminate()
                        await loop.run_in_executor(None, self._process.wait)
                    except Exception:
                        try:
                            self._process.kill()
                            await loop.run_in_executor(None, self._process.wait)
                        except Exception:
                            pass

            # Si es un archivo y no está en loop o si se pidió stop explícito, salir
            if self.kind == SourceKind.FILE or self._stop_event.is_set():
                break

            # Para MIC o STREAM, salida no solicitada -> estado degradado y reconexión
            logger.warning(
                "Fuente de audio en vivo finalizó o se desconectó. Reintentando en %.1fs...",
                backoff
            )
            if self.on_degraded:
                self.on_degraded()

            await asyncio.sleep(backoff)
            backoff = min(backoff * 1.5, max_backoff)

        # Señal de fin de flujo
        await self._enqueue_chunk(None)

    async def _enqueue_chunk(self, chunk: Optional[bytes]) -> None:
        """Encola un chunk. Si la cola está llena, descarta el más antiguo (backpressure)."""
        if chunk is not None and self._queue.full():
            try:
                self._queue.get_nowait()
                logger.warning("Buffer de audio lleno (50 chunks / 5s). Descartando chunk antiguo para mantener baja latencia.")
            except asyncio.QueueEmpty:
                pass

        await self._queue.put(chunk)

    async def stream_chunks(self) -> AsyncGenerator[bytes, None]:
        """
        Inicia la tarea del supervisor y produce un flujo de bloques PCM (3.200 bytes).
        """
        self._stop_event.clear()
        self._reader_task = asyncio.create_task(self._run_supervisor(), name=f"ffmpeg-supervisor-{self.kind}")

        start_time = asyncio.get_running_loop().time()
        bytes_yielded = 0

        try:
            while not self._stop_event.is_set():
                chunk = await self._queue.get()
                if chunk is None:
                    break
                yield chunk
                bytes_yielded += len(chunk)
        except asyncio.CancelledError:
            logger.info("Stream de audio cancelado por el llamador.")
            raise
        finally:
            await self.stop()
            elapsed = max(asyncio.get_running_loop().time() - start_time, 0.001)
            rate = bytes_yielded / elapsed
            logger.info(
                "AudioSource [%s] finalizado. Total bytes: %d, tiempo: %.2fs, tasa promedio: %.1f B/s",
                self.kind.value, bytes_yielded, elapsed, rate
            )

    async def stop(self) -> None:
        """Detiene de forma segura el subproceso y la tarea lectora."""
        self._stop_event.set()
        loop = asyncio.get_running_loop()
        if self._process and self._process.poll() is None:
            try:
                self._process.terminate()
                try:
                    await asyncio.wait_for(loop.run_in_executor(None, self._process.wait), timeout=1.5)
                except (asyncio.TimeoutError, Exception):
                    self._process.kill()
                    await asyncio.wait_for(loop.run_in_executor(None, self._process.wait), timeout=1.0)
            except Exception as e:
                logger.debug("Error cerrando proceso ffmpeg: %s", e)

        if self._reader_task and not self._reader_task.done():
            self._reader_task.cancel()
            try:
                await asyncio.wait_for(self._reader_task, timeout=1.0)
            except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
                pass
