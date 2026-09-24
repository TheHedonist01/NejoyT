import logging
import os
import site
import sys
from typing import Tuple

logger = logging.getLogger("asr.device")


def setup_accelerator_paths() -> None:
    """
    Configura rutas dinámicas de librerías nativas y DLLs de aceleración (NVIDIA / AMD)
    en Windows sin hardcodear rutas absolutas.
    """
    if sys.platform == "win32":
        for path in site.getsitepackages():
            for sub in [("nvidia", "cublas", "bin"), ("nvidia", "cudnn", "bin")]:
                p = os.path.join(path, *sub)
                if os.path.isdir(p):
                    try:
                        os.add_dll_directory(p)
                        os.environ["PATH"] = p + ";" + os.environ.get("PATH", "")
                    except Exception:
                        pass


def detect_compute_device() -> Tuple[str, str, int]:
    """
    Detecta automáticamente el mejor dispositivo de cómputo disponible en la máquina:
    - CUDA (NVIDIA) si está disponible -> ("cuda", "float16", 0)
    - ROCm (AMD) si está disponible -> ("cuda", "float16", 0)
    - MPS (Apple Silicon Metal) si está disponible -> ("mps", "float16", 0)
    - CPU (fallback universal con cuantización int8) -> ("cpu", "int8", 0)

    Retorna:
        Tuple[device_name, compute_type, device_index]
    """
    setup_accelerator_paths()

    # 1. Verificar CTranslate2 / CUDA / ROCm
    try:
        import ctranslate2
        cuda_count = ctranslate2.get_cuda_device_count()
        if cuda_count > 0:
            logger.info("Acelerador detectado: CUDA/ROCm disponible (%d dispositivo(s)).", cuda_count)
            return "cuda", "float16", 0
    except Exception as e:
        logger.debug("Verificación CTranslate2 CUDA: %s", e)

    # 2. Verificar PyTorch para MPS (Apple Silicon) o CUDA
    try:
        import torch
        if torch.cuda.is_available():
            is_rocm = getattr(torch.version, "hip", None) is not None
            hw_name = "ROCm" if is_rocm else "CUDA"
            logger.info("Acelerador detectado vía PyTorch: %s.", hw_name)
            return "cuda", "float16", 0

        if hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
            logger.info("Acelerador detectado: Apple Silicon MPS (Metal).")
            return "mps", "float16", 0
    except Exception as e:
        logger.debug("Verificación PyTorch hardware: %s", e)

    # 3. Fallback universal CPU
    logger.info("No se detectó GPU acelerada. Operando en modo universal: CPU (int8).")
    return "cpu", "int8", 0
