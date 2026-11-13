FROM gcc:14-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends clang-format cppcheck && rm -rf /var/lib/apt/lists/*
RUN mkdir /work && chown 65534:65534 /work && chmod 770 /work
WORKDIR /work
USER 65534:65534
