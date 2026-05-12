// arc_messages_video.c
// Encoder/decoder pairs for the VIDEO family payloads.

#include "arc_messages_video.h"

int arc_video_set_bitrate_encode(const arc_video_set_bitrate_t* msg,
                                 uint8_t* out, size_t out_capacity)
{
    if (msg == NULL || out == NULL) return ARC_ERR_BAD_ARG;
    if (out_capacity < ARC_VIDEO_SET_BITRATE_PAYLOAD_SIZE) return ARC_ERR_BUFFER;
    out[0] = (uint8_t)((msg->bitrate_bps >> 24) & 0xFF);
    out[1] = (uint8_t)((msg->bitrate_bps >> 16) & 0xFF);
    out[2] = (uint8_t)((msg->bitrate_bps >> 8) & 0xFF);
    out[3] = (uint8_t)(msg->bitrate_bps & 0xFF);
    return ARC_VIDEO_SET_BITRATE_PAYLOAD_SIZE;
}

arc_result_t arc_video_set_bitrate_decode(const uint8_t* in, size_t len,
                                          arc_video_set_bitrate_t* msg)
{
    if (in == NULL || msg == NULL) return ARC_ERR_BAD_ARG;
    if (len != ARC_VIDEO_SET_BITRATE_PAYLOAD_SIZE) return ARC_ERR_BAD_LENGTH;
    msg->bitrate_bps =
        ((uint32_t)in[0] << 24) |
        ((uint32_t)in[1] << 16) |
        ((uint32_t)in[2] << 8)  |
        ((uint32_t)in[3]);
    return ARC_OK;
}

int arc_video_status_report_encode(const arc_video_status_report_t* msg,
                                   uint8_t* out, size_t out_capacity)
{
    if (msg == NULL || out == NULL) return ARC_ERR_BAD_ARG;
    if (out_capacity < ARC_VIDEO_STATUS_REPORT_PAYLOAD_SIZE) return ARC_ERR_BUFFER;
    out[0] = msg->state;
    out[1] = msg->cpu_temp_c;
    out[2] = msg->cpu_load_pct;
    out[3] = (uint8_t)((msg->free_disk_mb >> 8) & 0xFF);
    out[4] = (uint8_t)(msg->free_disk_mb & 0xFF);
    out[5] = (uint8_t)msg->rssi_dbm;  // 2's complement preserved across cast
    out[6] = (uint8_t)((msg->tx_frames >> 8) & 0xFF);
    out[7] = (uint8_t)(msg->tx_frames & 0xFF);
    out[8] = (uint8_t)((msg->dropped_frames >> 8) & 0xFF);
    out[9] = (uint8_t)(msg->dropped_frames & 0xFF);
    return ARC_VIDEO_STATUS_REPORT_PAYLOAD_SIZE;
}

arc_result_t arc_video_status_report_decode(const uint8_t* in, size_t len,
                                            arc_video_status_report_t* msg)
{
    if (in == NULL || msg == NULL) return ARC_ERR_BAD_ARG;
    if (len != ARC_VIDEO_STATUS_REPORT_PAYLOAD_SIZE) return ARC_ERR_BAD_LENGTH;
    msg->state          = in[0];
    msg->cpu_temp_c     = in[1];
    msg->cpu_load_pct   = in[2];
    msg->free_disk_mb   = ((uint16_t)in[3] << 8) | (uint16_t)in[4];
    msg->rssi_dbm       = (int8_t)in[5];
    msg->tx_frames      = ((uint16_t)in[6] << 8) | (uint16_t)in[7];
    msg->dropped_frames = ((uint16_t)in[8] << 8) | (uint16_t)in[9];
    return ARC_OK;
}
