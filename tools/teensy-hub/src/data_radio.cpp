// data_radio.cpp -- see data_radio.h.

#include "data_radio.h"
#include "hub_config.h"

static uint32_t g_tx_count = 0;

void data_radio_begin(void) {
    HUB_DATA_RADIO_SERIAL.begin(HUB_DATA_RADIO_BAUD);
}

void data_radio_emit(const arc_frame_t* frame) {
    if (!frame || !frame->payload || frame->payload_len == 0) return;

    HUB_DATA_RADIO_SERIAL.write(frame->payload, frame->payload_len);
    HUB_DATA_RADIO_SERIAL.write((uint8_t)0);
    g_tx_count++;
}

void data_radio_link_send(void* user, const arc_frame_t* frame) {
    (void)user;
    data_radio_emit(frame);
}

uint32_t data_radio_tx_count(void) { return g_tx_count; }
