IMAGE ?= quay.io/REPLACE_ME/mikrotik-rds-csi:0.4.0
CONSOLE_IMAGE ?= quay.io/REPLACE_ME/mikrotik-rds-csi-console:0.4.0

.PHONY: generate test build build-console console-typecheck console-build

generate:
	bash ./hack/generate-proto.sh

test:
	python -m pytest -q

build:
	podman build -f Containerfile -t $(IMAGE) .

build-console:
	podman build -f Containerfile.console -t $(CONSOLE_IMAGE) .

console-typecheck:
	cd console-plugin && npm run typecheck

console-build:
	cd console-plugin && npm run build
