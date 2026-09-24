import asyncio
import logging
import os
import shutil
from typing import AsyncGenerator, Optional

logger = logging.getLogger("audio.source")


class AudioSource:
    """
    Subproceso FFmpeg que normaliza cualquier fuente de audio
    (archivo local, URL HTTP/HLS, stream RTMP o micrófono)
    a PCM s16le, 16.000 Hz, mono.
    """

    SAMPLE_RATE = 16000
    BYTES_PER_SAMPLE = 2  # 16-bit
    CHANNELS = 1
    BYTES_PER_SECOND = SAMPLE_RATE * BYTES_PER_SAMPLE * CHANNELS  # 32.000 B/s
    CHUNK_DURATION_SEC = 0.1  # 100 ms
    CHUNK_SIZE_BYTES = int(SAMPLE_RATE * CHUNK_DURATION_SEC * BYTES_PER_SAMPLE)  # 3.200 B

    def __init__(self, source_uri: str, is_live_stream: bool = False):
        """
        :param source_uri: Archivo local o URL de streaming.
        :param is_live_stream: Si es True, no aplica pacing adaptativo (el stream ya viene a tiempo real).
                               Si es False (ej. archivo de audio), regula el flujo a 32.000 B/s exactos.
        """
        self.source_uri = source_uri
        self.is_live_stream = is_live_stream
        self._process: Optional[asyncio.subprocess.Process] = None
        self._stop_event = asyncio.Event()

    def _resolve_ffmpeg_cmd(self) -> list[str]:
        # Buscar ejecutable de ffmpeg en PATH
        ffmpeg_bin = shutil.which("ffmpeg") or "ffmpeg"

        cmd = [
            ffmpeg_bin,
            "-hide_banner",
            "-loglevel", "error",
            "-i", self.source_uri,
            "-f", "s16le",
            "-acodec", "pcm_s16le",
            "-ar", str(self.SAMPLE_RATE),
            "-ac", str(self.CHANNELS),
            "-",
        ]
        return cmd

    async def stream_chunks(self) -> AsyncGenerator[bytes, None]:
        """
        Inicia el proceso FFmpeg y genera bloques PCM de 100 ms (3.200 bytes).
        """
        cmd = self._resolve_ffmpeg_cmd()
        logger.info("Iniciando FFmpeg con comando: %s", " ".join(cmd))

        self._process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )

        assert self._process.stdout is not None
        assert self._process.stderr is not None

        start_time = asyncio.get_running_loop().time()
        bytes_yielded = 0

        try:
            while not self._stop_event.is_set():
                try:
                    data = await self._process.stdout.readexactly(self.CHUNK_SIZE_BYTES)
                except asyncio.IncompleteReadError as e:
                    if e.partial:
                        yield e.partial
                        bytes_yielded += len(e.partial)
                    break

                if not data:
                    break

                yield data
                bytes_yielded += len(data)

                # Pacing adaptativo para archivos locales (evita adelanto temporal respecto al reloj real)
                if not self.is_live_stream:
                    expected_elapsed = bytes_yielded / self.BYTES_PER_SECOND
                    actual_elapsed = asyncio.get_running_loop().time() - start_time
                    delay = expected_elapsed - actual_elapsed
                    if delay > 0:
                        await asyncio.sleep(delay)

        except asyncio.CancelledError:
            logger.info("Stream de audio cancelado por el llamador.")
            raise
        finally:
            await self.stop()
            elapsed = max(asyncio.get_running_loop().time() - start_time, 0.001)
            rate = bytes_yielded / elapsed
            logger.info(
                "FFmpeg finalizado. Total bytes: %d, tiempo: %.2fs, tasa promedio: %.1f B/s (esperado ~32.000 B/s)",
                bytes_yielded, elapsed, rate
            )

    async def stop(self) -> None:
        """Detiene el subproceso limpiamente."""
        self._stop_event.set()
        if self._process and self._process.returncode is None:
            try:
                self._process.terminate()
                await asyncio.wait_for(self._process.wait(), timeout=2.0)
            except (asyncio.TimeoutError, ProcessLookupError):
                if self._process.returncode is None:
                    self._process.kill()
                    await self._process.wait()
