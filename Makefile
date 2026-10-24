# Makefile for lmstop

.PHONY: help buildwin buildwsl run clean chmod

help:
	@echo "lmstop Makefile"
	@echo "================="
	@echo "help       - Show this help message"
	@echo "buildwsl   - Build Linux/WSL standalone executable with PyInstaller"
	@echo "buildwin   - Build Windows standalone .exe with native Windows PyInstaller"
	@echo "run        - Run the Python script directly"
	@echo "clean      - Remove build artifacts"
	@echo "chmod      - Make lmstop.py executable"

# Locate the native Windows PyInstaller from WSL. Prefer a `pyinstaller.exe`
# on PATH, then the standard per-user Python Script folder.
WIN_PYINSTALLER := $(shell command -v pyinstaller.exe 2>/dev/null || \
	ls /mnt/c/Users/*/AppData/Local/Programs/Python/Python*/Scripts/pyinstaller.exe 2>/dev/null | head -1)

# Locate the LM Studio CLI: on PATH first, then the per-user LM Studio bin.
LMS := $(shell command -v lms.exe 2>/dev/null || command -v lms 2>/dev/null || \
	ls /mnt/c/Users/*/.lmstudio/bin/lms.exe 2>/dev/null | head -1)

buildwsl: lmstop.ico
	@echo "Building WSL/Linux standalone executable..."
	@if command -v pyinstaller >/dev/null 2>&1; then \
		pyinstaller --onefile --name lmstop lmstop.py && echo "Success: dist/lmstop"; \
	else \
		echo "Error: PyInstaller not found. Install with: pip install pyinstaller"; \
		exit 1; \
	fi

buildwin: lmstop.ico
	@echo "Building Windows standalone executable..."
	@if [ -n "$(WIN_PYINSTALLER)" ]; then \
		echo "Using native Windows PyInstaller: $(WIN_PYINSTALLER)"; \
		"$(WIN_PYINSTALLER)" --onefile --name lmstop --icon=lmstop.ico lmstop.py && echo "Success: dist/lmstop.exe"; \
	else \
		echo "Error: Windows pyinstaller.exe not found."; \
		echo "Install it on Windows with: pip install pyinstaller"; \
		echo "Then either add it to PATH or place it in your Python Scripts folder."; \
		exit 1; \
	fi

run:
	@if [ -n "$(LMS)" ]; then \
		echo "Running lmstop (lms: $(LMS))..."; \
		python3 lmstop.py --lms-path "$(LMS)"; \
	elif command -v python3 >/dev/null 2>&1; then \
		echo "Running lmstop..."; \
		python3 lmstop.py; \
	else \
		echo "Error: python3 not found"; \
		exit 1; \
	fi

clean:
	@echo "Removing build artifacts..."
	@rm -rf dist/ build/ *.spec __pycache__/
	@echo "Cleanup complete."

# Generate a real Windows icon. A text placeholder is invalid and breaks
# PyInstaller's icon embedding, so we draw a small 256x256 memory-bar glyph.
lmstop.ico: scripts/gen_icon.py
	@echo "Generating lmstop.ico..."
	@python3 scripts/gen_icon.py
	@echo "Done: lmstop.ico"

chmod:
	@chmod +x lmstop.py
	@echo "Made lmstop.py executable."