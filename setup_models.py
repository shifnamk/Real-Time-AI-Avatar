"""
Download all large models for Avatar Real-Time System
Run this after extracting the code package
"""

import os
import sys
from pathlib import Path
from huggingface_hub import hf_hub_download
import urllib.request
import shutil

def download_file(url, destination):
    """Download file with progress"""
    print(f"Downloading {destination}...")
    urllib.request.urlretrieve(url, destination)
    print(f"✓ Downloaded {destination}")

def setup_models():
    print("=" * 60)
    print("Setting up Avatar Models")
    print("=" * 60)
    print()
    
    base_dir = Path(__file__).parent
    
    # Create model directories
    models_dir = base_dir / "livetalking" / "models"
    models_dir.mkdir(parents=True, exist_ok=True)
    
    print("Step 1/6: Downloading MuseTalk V1.5 (UNet - 3.2 GB)...")
    musetalk_dir = models_dir / "musetalkV15"
    musetalk_dir.mkdir(exist_ok=True)
    
    # Download MuseTalk V1.5 UNet model
    hf_hub_download(
        repo_id="TMElyralab/MuseTalk",
        filename="unet.pth",
        subfolder="musetalkV15",
        local_dir=str(musetalk_dir.parent),
        local_dir_use_symlinks=False
    )
    hf_hub_download(
        repo_id="TMElyralab/MuseTalk",
        filename="musetalk.json",
        subfolder="musetalkV15",
        local_dir=str(musetalk_dir.parent),
        local_dir_use_symlinks=False
    )
    print("✓ MuseTalk V1.5 downloaded")
    print()
    
    print("Step 2/6: Downloading Whisper Tiny (74 MB)...")
    whisper_dir = models_dir / "whisper"
    whisper_dir.mkdir(exist_ok=True)
    
    # Download Whisper tiny.pt model
    download_file(
        "https://openaipublic.azureedge.net/main/whisper/models/65147644a518d12f04e32d6f3b26facc3f8dd46e5390956a9424a650c0ce22b9/tiny.pt",
        whisper_dir / "tiny.pt"
    )
    print("✓ Whisper Tiny downloaded")
    print()
    
    print("Step 3/6: Downloading VAE (SD-VAE-FT-MSE - 319 MB)...")
    vae_dir = models_dir / "sd-vae-ft-mse"
    vae_dir.mkdir(exist_ok=True)
    
    hf_hub_download(
        repo_id="stabilityai/sd-vae-ft-mse",
        filename="diffusion_pytorch_model.safetensors",
        local_dir=str(vae_dir),
        local_dir_use_symlinks=False
    )
    hf_hub_download(
        repo_id="stabilityai/sd-vae-ft-mse",
        filename="config.json",
        local_dir=str(vae_dir),
        local_dir_use_symlinks=False
    )
    print("✓ VAE downloaded")
    print()
    
    print("Step 4/6: Downloading DWPose (388 MB, for asset creation)...")
    dwpose_dir = models_dir / "dwpose"
    dwpose_dir.mkdir(exist_ok=True)
    
    download_file(
        "https://download.openmmlab.com/mmpose/v1/projects/rtmposev1/onnx_sdk/dw-ll_ucoco_384.pth",
        dwpose_dir / "dw-ll_ucoco_384.pth"
    )
    print("✓ DWPose downloaded")
    print()
    
    print("Step 5/6: Downloading Face Parsing Models (for asset creation)...")
    faceparse_dir = models_dir / "face-parse-bisent"
    faceparse_dir.mkdir(exist_ok=True)
    
    hf_hub_download(
        repo_id="jonathandinu/face-parsing",
        filename="79999_iter.pth",
        local_dir=str(faceparse_dir),
        local_dir_use_symlinks=False
    )
    download_file(
        "https://download.pytorch.org/models/resnet18-5c106cde.pth",
        faceparse_dir / "resnet18-5c106cde.pth"
    )
    print("✓ Face Parsing models downloaded")
    print()
    
    print("Step 6/6: Downloading MuseTalk UNet config...")
    musetalk_config = musetalk_dir / "musetalk.json"
    if not musetalk_config.exists():
        hf_hub_download(
            repo_id="TMElyralab/MuseTalk",
            filename="musetalk.json",
            subfolder="musetalkV15",
            local_dir=str(musetalk_dir.parent),
            local_dir_use_symlinks=False
        )
    print("✓ MuseTalk config downloaded")
    print()
    
    print("=" * 60)
    print("✅ All models downloaded successfully!")
    print("=" * 60)
    print()
    print("Next steps:")
    print("1. Install Python dependencies: pip install -r requirements.txt")
    print("2. Install Node.js dependencies: cd avatar-frontend && npm install")
    print("3. Start the system: cd avatar_agent && start_livetalking.bat")
    print()

if __name__ == "__main__":
    try:
        # Check if huggingface_hub is installed
        import huggingface_hub
    except ImportError:
        print("Installing huggingface_hub...")
        os.system(f"{sys.executable} -m pip install huggingface_hub")
        import huggingface_hub
    
    setup_models()

