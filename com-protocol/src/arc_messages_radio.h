// arc_messages_radio.h
// Payload helpers for the RADIO family (FAMILY = 0x04).
//
// Direction: anyone -> radio for the command types (SET_FREQUENCY,
// SET_TX_POWER, GET_STATUS); radio -> originator for STATUS_REPORT.
//
// The rocket-side radios both hang off the Teensy hub:
//   ARC_ADDR_RADIO_CMD  (0x20) -- command/status link; full ARC participant.
//   ARC_ADDR_RADIO_DATA (0x22) -- live data downlink. It speaks a proprietary,
//                                 non-ARC protocol, so the hub TERMINATES frames
//                                 addressed to it and transcodes them into the
//                                 vendor format (see tools/teensy-hub/data_radio).
//                                 The message types below do NOT apply to it.
// ARC_ADDR_RADIO_G (0x21) is the ground-side OTA peer of RADIO_CMD.
// A radio module also acts as a forwarding bridge for over-the-air frames;
// this catalog only covers self-addressed control/status on ARC radios.

#ifndef ARC_MESSAGES_RADIO_H
#define ARC_MESSAGES_RADIO_H

#include "arc_protocol.h"

#ifdef __cplusplus
extern "C" {
#endif

// ----------------------------------------------------------------------
// RADIO message types.
// ----------------------------------------------------------------------
#define ARC_RADIO_SET_FREQUENCY  0x01  // arc_radio_set_frequency_t
#define ARC_RADIO_SET_TX_POWER   0x02  // arc_radio_set_tx_power_t
#define ARC_RADIO_GET_STATUS     0x03  // empty payload
#define ARC_RADIO_STATUS_REPORT  0x10  // arc_radio_status_report_t

// ----------------------------------------------------------------------
// RADIO_STATUS_REPORT.error_flags bits.
// ----------------------------------------------------------------------
#define ARC_RADIO_ERR_TX_BUSY     0x01  // last TX still in flight
#define ARC_RADIO_ERR_PLL_UNLOCK  0x02  // synthesizer not locked at programmed freq
#define ARC_RADIO_ERR_OVERTEMP    0x04  // PA shut down for thermal protection
#define ARC_RADIO_ERR_RX_OVERRUN  0x08  // dropped a packet because the host wasn't draining

// ----------------------------------------------------------------------
// SET_FREQUENCY: 4-byte big-endian unsigned, in Hz.
// Range covers anything we'd realistically use (DC..4.29 GHz).
// ----------------------------------------------------------------------
#define ARC_RADIO_SET_FREQUENCY_PAYLOAD_SIZE 4

typedef struct {
    uint32_t frequency_hz;
} arc_radio_set_frequency_t;

int arc_radio_set_frequency_encode(const arc_radio_set_frequency_t* msg,
                                   uint8_t* out, size_t out_capacity);
arc_result_t arc_radio_set_frequency_decode(const uint8_t* in, size_t len,
                                            arc_radio_set_frequency_t* msg);

// ----------------------------------------------------------------------
// SET_TX_POWER: 1 signed byte in dBm. Negative values are valid for
// low-power test modes; the radio clamps to its hardware range.
// ----------------------------------------------------------------------
#define ARC_RADIO_SET_TX_POWER_PAYLOAD_SIZE 1

typedef struct {
    int8_t tx_power_dbm;
} arc_radio_set_tx_power_t;

int arc_radio_set_tx_power_encode(const arc_radio_set_tx_power_t* msg,
                                  uint8_t* out, size_t out_capacity);
arc_result_t arc_radio_set_tx_power_decode(const uint8_t* in, size_t len,
                                           arc_radio_set_tx_power_t* msg);

// ----------------------------------------------------------------------
// STATUS_REPORT: 12-byte fixed snapshot of the radio's current state.
//
// Wire layout:
//   [frequency_hz   u32 BE]
//   [tx_power_dbm   i8]
//   [rssi_dbm       i8]   -- last received frame's RSSI
//   [snr_db         i8]   -- last received frame's SNR
//   [error_flags    u8]   -- ARC_RADIO_ERR_* bits
//   [packets_rx     u16 BE]
//   [packets_tx     u16 BE]
// ----------------------------------------------------------------------
#define ARC_RADIO_STATUS_REPORT_PAYLOAD_SIZE 12

typedef struct {
    uint32_t frequency_hz;
    int8_t   tx_power_dbm;
    int8_t   rssi_dbm;
    int8_t   snr_db;
    uint8_t  error_flags;
    uint16_t packets_rx;
    uint16_t packets_tx;
} arc_radio_status_report_t;

int arc_radio_status_report_encode(const arc_radio_status_report_t* msg,
                                   uint8_t* out, size_t out_capacity);
arc_result_t arc_radio_status_report_decode(const uint8_t* in, size_t len,
                                            arc_radio_status_report_t* msg);

#ifdef __cplusplus
}
#endif

#endif  // ARC_MESSAGES_RADIO_H
