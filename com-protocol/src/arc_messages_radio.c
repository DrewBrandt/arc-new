// arc_messages_radio.c
// Encoder/decoder pairs for the RADIO family payloads.

#include "arc_messages_radio.h"

int arc_radio_set_frequency_encode(const arc_radio_set_frequency_t* msg,
                                   uint8_t* out, size_t out_capacity)
{
    if (msg == NULL || out == NULL) return ARC_ERR_BAD_ARG;
    if (out_capacity < ARC_RADIO_SET_FREQUENCY_PAYLOAD_SIZE) return ARC_ERR_BUFFER;
    out[0] = (uint8_t)((msg->frequency_hz >> 24) & 0xFF);
    out[1] = (uint8_t)((msg->frequency_hz >> 16) & 0xFF);
    out[2] = (uint8_t)((msg->frequency_hz >> 8)  & 0xFF);
    out[3] = (uint8_t)(msg->frequency_hz & 0xFF);
    return ARC_RADIO_SET_FREQUENCY_PAYLOAD_SIZE;
}

arc_result_t arc_radio_set_frequency_decode(const uint8_t* in, size_t len,
                                            arc_radio_set_frequency_t* msg)
{
    if (in == NULL || msg == NULL) return ARC_ERR_BAD_ARG;
    if (len != ARC_RADIO_SET_FREQUENCY_PAYLOAD_SIZE) return ARC_ERR_BAD_LENGTH;
    msg->frequency_hz =
        ((uint32_t)in[0] << 24) |
        ((uint32_t)in[1] << 16) |
        ((uint32_t)in[2] << 8)  |
        ((uint32_t)in[3]);
    return ARC_OK;
}

int arc_radio_set_tx_power_encode(const arc_radio_set_tx_power_t* msg,
                                  uint8_t* out, size_t out_capacity)
{
    if (msg == NULL || out == NULL) return ARC_ERR_BAD_ARG;
    if (out_capacity < ARC_RADIO_SET_TX_POWER_PAYLOAD_SIZE) return ARC_ERR_BUFFER;
    out[0] = (uint8_t)msg->tx_power_dbm;  // 2's complement preserved
    return ARC_RADIO_SET_TX_POWER_PAYLOAD_SIZE;
}

arc_result_t arc_radio_set_tx_power_decode(const uint8_t* in, size_t len,
                                           arc_radio_set_tx_power_t* msg)
{
    if (in == NULL || msg == NULL) return ARC_ERR_BAD_ARG;
    if (len != ARC_RADIO_SET_TX_POWER_PAYLOAD_SIZE) return ARC_ERR_BAD_LENGTH;
    msg->tx_power_dbm = (int8_t)in[0];
    return ARC_OK;
}

int arc_radio_status_report_encode(const arc_radio_status_report_t* msg,
                                   uint8_t* out, size_t out_capacity)
{
    if (msg == NULL || out == NULL) return ARC_ERR_BAD_ARG;
    if (out_capacity < ARC_RADIO_STATUS_REPORT_PAYLOAD_SIZE) return ARC_ERR_BUFFER;
    out[0]  = (uint8_t)((msg->frequency_hz >> 24) & 0xFF);
    out[1]  = (uint8_t)((msg->frequency_hz >> 16) & 0xFF);
    out[2]  = (uint8_t)((msg->frequency_hz >> 8)  & 0xFF);
    out[3]  = (uint8_t)(msg->frequency_hz & 0xFF);
    out[4]  = (uint8_t)msg->tx_power_dbm;
    out[5]  = (uint8_t)msg->rssi_dbm;
    out[6]  = (uint8_t)msg->snr_db;
    out[7]  = msg->error_flags;
    out[8]  = (uint8_t)((msg->packets_rx >> 8) & 0xFF);
    out[9]  = (uint8_t)(msg->packets_rx & 0xFF);
    out[10] = (uint8_t)((msg->packets_tx >> 8) & 0xFF);
    out[11] = (uint8_t)(msg->packets_tx & 0xFF);
    return ARC_RADIO_STATUS_REPORT_PAYLOAD_SIZE;
}

arc_result_t arc_radio_status_report_decode(const uint8_t* in, size_t len,
                                            arc_radio_status_report_t* msg)
{
    if (in == NULL || msg == NULL) return ARC_ERR_BAD_ARG;
    if (len != ARC_RADIO_STATUS_REPORT_PAYLOAD_SIZE) return ARC_ERR_BAD_LENGTH;
    msg->frequency_hz =
        ((uint32_t)in[0] << 24) |
        ((uint32_t)in[1] << 16) |
        ((uint32_t)in[2] << 8)  |
        ((uint32_t)in[3]);
    msg->tx_power_dbm = (int8_t)in[4];
    msg->rssi_dbm     = (int8_t)in[5];
    msg->snr_db       = (int8_t)in[6];
    msg->error_flags  = in[7];
    msg->packets_rx   = ((uint16_t)in[8]  << 8) | (uint16_t)in[9];
    msg->packets_tx   = ((uint16_t)in[10] << 8) | (uint16_t)in[11];
    return ARC_OK;
}
