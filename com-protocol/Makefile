# Makefile for arc-protocol library and tests.

CC      ?= cc
CFLAGS  ?= -std=c11 -Wall -Wextra -Wpedantic -O2 -g
CFLAGS  += -Isrc

SRC     := src/arc_protocol.c
HDR     := src/arc_protocol.h
TEST    := test/test_arc_protocol.c

.PHONY: all test clean

all: test

test: build/test_arc_protocol
	./build/test_arc_protocol

build/test_arc_protocol: $(SRC) $(HDR) $(TEST) | build
	$(CC) $(CFLAGS) -o $@ $(SRC) $(TEST)

build:
	mkdir -p build

clean:
	rm -rf build
