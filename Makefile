SHELL := /bin/bash

.PHONY: build-receiver build-worker clean-worker build-worker-docker

build-receiver:
	@mkdir -p build
	# Pack with directory structure preserved (module path: src/aws_lambda/receiver.py)
	@zip -r build/receiver.zip src/aws_lambda/receiver.py >/dev/null
	@echo "Built build/receiver.zip"

clean-worker:
	@rm -rf build/worker

build-worker: clean-worker
	@mkdir -p build/worker
	# Include project sources under src/
	@rsync -a src/ build/worker/src/
	# Install runtime deps for Lambda into the package root
	@python -m pip install --no-cache-dir --upgrade -r requirements_lambda.txt -t build/worker
	# Zip everything (sources + site-packages)
	@cd build/worker && zip -r ../worker.zip . >/dev/null
	@echo "Built build/worker.zip"

# Build inside Amazon Linux (SAM) image to ensure manylinux wheels for Lambda
# Usage: make build-worker-docker [PY=3.11]
build-worker-docker:
	@PY=$${PY:-3.11}; \
	IMG=public.ecr.aws/sam/build-python$$PY; \
	echo "Using image $$IMG"; \
	docker run --rm -v "$$PWD":/var/task -w /var/task $$IMG bash -lc "set -euo pipefail; rm -rf build/worker; mkdir -p build/worker; command -v rsync >/dev/null 2>&1 || (yum -y install rsync >/dev/null 2>&1); rsync -a src/ build/worker/src/; python -m pip install --upgrade pip >/dev/null; python -m pip install --no-cache-dir -r requirements_lambda.txt -t build/worker >/dev/null; cd build/worker && zip -r ../worker.zip . >/dev/null; echo 'Built build/worker.zip (Docker)'"
