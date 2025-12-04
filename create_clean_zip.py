"""
Create clean zip file without models and dependencies
Fast and simple approach
"""

import zipfile
import os
from pathlib import Path

def should_exclude(path_str):
    """Check if path should be excluded"""
    exclude_patterns = [
        '.venv', 'node_modules', '__pycache__', '.git',
        'models', 'dist', '.pyc'
    ]
    return any(pattern in path_str for pattern in exclude_patterns)

def create_clean_zip():
    source_dir = Path(r"C:\shifna\avatar_backup_v1")
    dest_zip = Path(r"F:\avatar_v2_clean.zip")
    
    print("Creating clean zip file...")
    print(f"Source: {source_dir}")
    print(f"Destination: {dest_zip}")
    print()
    
    file_count = 0
    
    with zipfile.ZipFile(dest_zip, 'w', zipfile.ZIP_DEFLATED, compresslevel=5) as zipf:
        for root, dirs, files in os.walk(source_dir):
            # Remove excluded directories from traversal
            dirs[:] = [d for d in dirs if not should_exclude(d)]
            
            for file in files:
                file_path = Path(root) / file
                rel_path = file_path.relative_to(source_dir.parent)
                
                # Skip excluded files
                if should_exclude(str(rel_path)):
                    continue
                
                try:
                    zipf.write(file_path, rel_path)
                    file_count += 1
                    if file_count % 100 == 0:
                        print(f"  Added {file_count} files...")
                except Exception as e:
                    print(f"  Skipped: {rel_path} ({e})")
    
    # Get final size
    size_mb = dest_zip.stat().st_size / (1024 * 1024)
    
    print()
    print("=" * 50)
    print("SUCCESS!")
    print("=" * 50)
    print(f"File: {dest_zip}")
    print(f"Size: {size_mb:.2f} MB")
    print(f"Files: {file_count}")
    print()
    print("Excluded:")
    print("  - AI models (~9 GB)")
    print("  - Python venv (~4 GB)")
    print("  - Node modules (~100 MB)")
    print()
    print("To setup after extraction:")
    print("  1. python setup_models.py")
    print("  2. pip install -r requirements.txt")
    print("  3. cd avatar-frontend && npm install")

if __name__ == "__main__":
    create_clean_zip()

