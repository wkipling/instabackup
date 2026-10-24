# Build and test helpers.  `make build` needs Zig (or the vendored copy in
# vendor/pyzig/ziglang/zig).
.PHONY: all build test certs container clean

ZIG ?= $(shell command -v zig 2>/dev/null)

all: build

build:
	@if [ -z "$(ZIG)" ]; then echo "zig not found; set ZIG=/path/to/zig" >&2; exit 1; fi
	ZIG="$(ZIG)" sh camera/build_insupload.sh
	ZIG="$(ZIG)" sh camera/build_tailrelay.sh
	ZIG="$(ZIG)" sh camera/build_x4preload.sh
	python3 camera/cloudkick_build.py camera/cloudkick
	chmod +x camera/cloudkick

certs:
	sh camera/make_certs.sh camera/certs 127.0.0.1

test: build
	cd camera && python3 test_setup_tui.py
	cd camera && python3 test_wifi_connect.py
	cd camera && python3 test_sdcard_push.py
	cd camera && python3 test_install_x4air.py
	cd camera && python3 test_cloudkick.py
	cd camera && python3 test_tailrelay.py
	cd camera && python3 test_x4preload.py
	cd camera && python3 test_x4_bootstrap.py

container:
	docker build -f server/Dockerfile -t instabackup .

clean:
	rm -f camera/insupload camera/insupload-host camera/tailrelay \
	      camera/tailrelay-host camera/cloudkick camera/libcloudbootstrap.so
	rm -rf camera/build
