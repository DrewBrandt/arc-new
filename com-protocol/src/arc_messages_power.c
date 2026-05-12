// arc_messages_power.c
// Encoder/decoder pairs for the POWER family payloads.

#include "arc_messages_power.h"

int arc_power_set_output_encode(const arc_power_set_output_t* msg,
                                uint8_t* out, size_t out_capacity)
{
    if (msg == NULL || out == NULL) return ARC_ERR_BAD_ARG;
    if (out_capacity < ARC_POWER_SET_OUTPUT_PAYLOAD_SIZE) return ARC_ERR_BUFFER;
    // Reject anything beyond the documented OFF/ON values; fault bits
    // are status-only and the upper reserved range is forbidden.
    if (msg->state != ARC_POWER_OFF && msg->state != ARC_POWER_ON) {
        return ARC_ERR_BAD_ARG;
    }
    out[0] = msg->channel;
    out[1] = msg->state;
    return ARC_POWER_SET_OUTPUT_PAYLOAD_SIZE;
}

arc_result_t arc_power_set_output_decode(const uint8_t* in, size_t len,
                                         arc_power_set_output_t* msg)
{
    if (in == NULL || msg == NULL) return ARC_ERR_BAD_ARG;
    if (len != ARC_POWER_SET_OUTPUT_PAYLOAD_SIZE) return ARC_ERR_BAD_LENGTH;
    msg->channel = in[0];
    msg->state   = in[1];
    return ARC_OK;
}

int arc_power_set_output_mask_encode(const arc_power_set_output_mask_t* msg,
                                     uint8_t* out, size_t out_capacity)
{
    if (msg == NULL || out == NULL) return ARC_ERR_BAD_ARG;
    if (out_capacity < ARC_POWER_SET_OUTPUT_MASK_PAYLOAD_SIZE) return ARC_ERR_BUFFER;
    out[0] = msg->enable_mask;
    out[1] = msg->state_mask;
    return ARC_POWER_SET_OUTPUT_MASK_PAYLOAD_SIZE;
}

arc_result_t arc_power_set_output_mask_decode(const uint8_t* in, size_t len,
                                              arc_power_set_output_mask_t* msg)
{
    if (in == NULL || msg == NULL) return ARC_ERR_BAD_ARG;
    if (len != ARC_POWER_SET_OUTPUT_MASK_PAYLOAD_SIZE) return ARC_ERR_BAD_LENGTH;
    msg->enable_mask = in[0];
    msg->state_mask  = in[1];
    return ARC_OK;
}

int arc_power_status_report_encode(const arc_power_status_report_t* msg,
                                   uint8_t* out, size_t out_capacity)
{
    if (msg == NULL || out == NULL) return ARC_ERR_BAD_ARG;
    if (msg->channel_count > ARC_POWER_MAX_CHANNELS) return ARC_ERR_BAD_ARG;

    size_t needed = ARC_POWER_STATUS_REPORT_SIZE(msg->channel_count);
    if (needed > ARC_MAX_PAYLOAD_SIZE) return ARC_ERR_TOO_LONG;
    if (out_capacity < needed) return ARC_ERR_BUFFER;

    size_t off = 0;
    out[off++] = msg->channel_count;
    for (uint8_t i = 0; i < msg->channel_count; i++) {
        out[off++] = msg->channels[i].state;
        out[off++] = (uint8_t)((msg->channels[i].current_ma >> 8) & 0xFF);
        out[off++] = (uint8_t)(msg->channels[i].current_ma & 0xFF);
    }
    out[off++] = (uint8_t)((msg->bus_voltage_mv >> 8) & 0xFF);
    out[off++] = (uint8_t)(msg->bus_voltage_mv & 0xFF);
    out[off++] = (uint8_t)msg->temp_c;
    return (int)off;
}

arc_result_t arc_power_status_report_decode(const uint8_t* in, size_t len,
                                            arc_power_status_report_t* msg)
{
    if (in == NULL || msg == NULL) return ARC_ERR_BAD_ARG;
    if (len < 1) return ARC_ERR_BAD_LENGTH;

    uint8_t channel_count = in[0];
    if (channel_count > ARC_POWER_MAX_CHANNELS) return ARC_ERR_TOO_LONG;
    if (len != ARC_POWER_STATUS_REPORT_SIZE(channel_count)) return ARC_ERR_BAD_LENGTH;

    msg->channel_count = channel_count;
    size_t off = 1;
    for (uint8_t i = 0; i < channel_count; i++) {
        msg->channels[i].state      = in[off++];
        msg->channels[i].current_ma = ((uint16_t)in[off] << 8) | (uint16_t)in[off + 1];
        off += 2;
    }
    msg->bus_voltage_mv = ((uint16_t)in[off] << 8) | (uint16_t)in[off + 1];
    off += 2;
    msg->temp_c = (int8_t)in[off];
    return ARC_OK;
}
