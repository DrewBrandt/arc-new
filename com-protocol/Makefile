# Makefile for arc-protocol library and tests.

CC      ?= cc
CFLAGS  ?= -std=c11 -Wall -Wextra -Wpedantic -O2 -g
CFLAGS  += -Isrc

PROTO_SRC := src/arc_protocol.c
PROTO_HDR := src/arc_protocol.h

RELIABLE_SRC := src/arc_reliable.c
RELIABLE_HDR := src/arc_reliable.h

ROUTER_SRC := src/arc_router.c
ROUTER_HDR := src/arc_router.h

LIB_SRC := $(PROTO_SRC) $(RELIABLE_SRC) $(ROUTER_SRC)
LIB_HDR := $(PROTO_HDR) $(RELIABLE_HDR) $(ROUTER_HDR)

PROTO_TEST    := test/test_arc_protocol.c
RELIABLE_TEST := test/test_arc_reliable.c
ROUTER_TEST   := test/test_arc_router.c

GEN := examples/gen_vectors.c

.PHONY: all test vectors clean

all: test

test: build/test_arc_protocol build/test_arc_reliable build/test_arc_router
	./build/test_arc_protocol
	./build/test_arc_reliable
	./build/test_arc_router

vectors: build/gen_vectors
	./build/gen_vectors

build/test_arc_protocol: $(PROTO_SRC) $(PROTO_HDR) $(PROTO_TEST) | build
	$(CC) $(CFLAGS) -o $@ $(PROTO_SRC) $(PROTO_TEST)

build/test_arc_reliable: $(LIB_SRC) $(LIB_HDR) $(RELIABLE_TEST) | build
	$(CC) $(CFLAGS) -o $@ $(PROTO_SRC) $(RELIABLE_SRC) $(RELIABLE_TEST)

build/test_arc_router: $(LIB_SRC) $(LIB_HDR) $(ROUTER_TEST) | build
	$(CC) $(CFLAGS) -o $@ $(PROTO_SRC) $(ROUTER_SRC) $(ROUTER_TEST)

build/gen_vectors: $(PROTO_SRC) $(PROTO_HDR) $(GEN) | build
	$(CC) $(CFLAGS) -o $@ $(PROTO_SRC) $(GEN)

build:
	mkdir -p build

clean:
	rm -rf build
