IMAGE ?= quay.io/REPLACE_ME/mikrotik-rds-csi:0.2.0

.PHONY: generate test build

generate:
	bash ./hack/generate-proto.sh

test:
	python -m pytest -q

build:
	podman build -f Containerfile -t $(IMAGE) .
