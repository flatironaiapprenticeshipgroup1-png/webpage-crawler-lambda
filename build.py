import os
import shutil
import subprocess

# Clean up previous build artifacts
for path in ["python", "function.zip", "layer.zip"]:
    if os.path.isdir(path):
        shutil.rmtree(path)
    elif os.path.isfile(path):
        os.remove(path)

# Install dependencies into python/ folder for Lambda Layer
subprocess.run(["pip", "install", "-r", "requirements.txt", "-t", "python/"], check=True)

# Zip the python/ folder into layer.zip
subprocess.run(["zip", "-r", "layer.zip", "python/"], check=True)

# Zip the function code into function.zip
subprocess.run(["zip", "function.zip", "handler.py", "requirements.txt"], check=True)

print("Done! layer.zip and function.zip are ready.")
