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

MESSAGES_SRC := src/arc_messages_netmgmt.c src/arc_messages_video.c src/arc_messages_fc_video.c \
                src/arc_messages_radio.c src/arc_messages_power.c
MESSAGES_HDR := src/arc_messages_netmgmt.h src/arc_messages_video.h src/arc_messages_fc_video.h \
                src/arc_messages_radio.h src/arc_messages_power.h

LIB_SRC := $(PROTO_SRC) $(RELIABLE_SRC) $(ROUTER_SRC) $(MESSAGES_SRC)
LIB_HDR := $(PROTO_HDR) $(RELIABLE_HDR) $(ROUTER_HDR) $(MESSAGES_HDR)

PROTO_TEST    := test/test_arc_protocol.c
RELIABLE_TEST := test/test_arc_reliable.c
ROUTER_TEST   := test/test_arc_router.c
MESSAGES_TEST := test/test_arc_messages.c

GEN := examples/gen_vectors.c

.PHONY: all test vectors clean

all: test

test: build/test_arc_protocol build/test_arc_reliable build/test_arc_router build/test_arc_messages
	./build/test_arc_protocol
	./build/test_arc_reliable
	./build/test_arc_router
	./build/test_arc_messages

vectors: build/gen_vectors
	./build/gen_vectors

build/test_arc_protocol: $(PROTO_SRC) $(PROTO_HDR) $(PROTO_TEST) | build
	$(CC) $(CFLAGS) -o $@ $(PROTO_SRC) $(PROTO_TEST)

build/test_arc_reliable: $(LIB_SRC) $(LIB_HDR) $(RELIABLE_TEST) | build
	$(CC) $(CFLAGS) -o $@ $(PROTO_SRC) $(RELIABLE_SRC) $(RELIABLE_TEST)

build/test_arc_router: $(LIB_SRC) $(LIB_HDR) $(ROUTER_TEST) | build
	$(CC) $(CFLAGS) -o $@ $(PROTO_SRC) $(ROUTER_SRC) $(ROUTER_TEST)

build/test_arc_messages: $(LIB_SRC) $(LIB_HDR) $(MESSAGES_TEST) | build
	$(CC) $(CFLAGS) -o $@ $(MESSAGES_SRC) $(MESSAGES_TEST)

build/gen_vectors: $(PROTO_SRC) $(PROTO_HDR) $(GEN) | build
	$(CC) $(CFLAGS) -o $@ $(PROTO_SRC) $(GEN)

build:
	mkdir -p build

clean:
	rm -rf build
