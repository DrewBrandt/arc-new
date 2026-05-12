// arc_messages_fc_video.c
// Encoder/decoder pairs for the FC_VIDEO family payloads.

#include "arc_messages_fc_video.h"

#include <string.h>

int arc_fc_video_set_layout_encode(const arc_fc_video_set_layout_t* msg,
                                   uint8_t* out, size_t out_capacity)
{
    if (msg == NULL || out == NULL) return ARC_ERR_BAD_ARG;
    if (out_capacity < ARC_FC_VIDEO_SET_LAYOUT_PAYLOAD_SIZE) return ARC_ERR_BUFFER;
    out[0] = msg->layout_id;
    return ARC_FC_VIDEO_SET_LAYOUT_PAYLOAD_SIZE;
}

arc_result_t arc_fc_video_set_layout_decode(const uint8_t* in, size_t len,
                                            arc_fc_video_set_layout_t* msg)
{
    if (in == NULL || msg == NULL) return ARC_ERR_BAD_ARG;
    if (len != ARC_FC_VIDEO_SET_LAYOUT_PAYLOAD_SIZE) return ARC_ERR_BAD_LENGTH;
    msg->layout_id = in[0];
    return ARC_OK;
}

int arc_fc_video_set_source_encode(const arc_fc_video_set_source_t* msg,
                                   uint8_t* out, size_t out_capacity)
{
    if (msg == NULL || out == NULL) return ARC_ERR_BAD_ARG;
    if (out_capacity < ARC_FC_VIDEO_SET_SOURCE_PAYLOAD_SIZE) return ARC_ERR_BUFFER;
    out[0] = msg->slot_id;
    out[1] = msg->sender_addr;
    return ARC_FC_VIDEO_SET_SOURCE_PAYLOAD_SIZE;
}

arc_result_t arc_fc_video_set_source_decode(const uint8_t* in, size_t len,
                                            arc_fc_video_set_source_t* msg)
{
    if (in == NULL || msg == NULL) return ARC_ERR_BAD_ARG;
    if (len != ARC_FC_VIDEO_SET_SOURCE_PAYLOAD_SIZE) return ARC_ERR_BAD_LENGTH;
    msg->slot_id     = in[0];
    msg->sender_addr = in[1];
    return ARC_OK;
}

int arc_fc_video_set_overlay_encode(const char* text, size_t text_len,
                                    uint8_t* out, size_t out_capacity)
{
    if (out == NULL || (text == NULL && text_len > 0)) return ARC_ERR_BAD_ARG;
    if (text_len + 1 > ARC_MAX_PAYLOAD_SIZE) return ARC_ERR_TOO_LONG;
    if (out_capacity < text_len + 1) return ARC_ERR_BUFFER;
    if (text_len > 0) memcpy(out, text, text_len);
    out[text_len] = '\0';
    return (int)(text_len + 1);
}

int arc_fc_video_set_overlay_decode(const uint8_t* in, size_t len,
                                    char* out, size_t out_capacity)
{
    if (in == NULL || out == NULL) return ARC_ERR_BAD_ARG;
    if (len == 0 || in[len - 1] != '\0') return ARC_ERR_BAD_LENGTH;
    if (out_capacity < len) return ARC_ERR_BUFFER;
    memcpy(out, in, len);
    return (int)(len - 1);  // string length without the NUL
}

int arc_fc_video_status_report_encode(const arc_fc_video_status_report_t* msg,
                                      uint8_t* out, size_t out_capacity)
{
    if (msg == NULL || out == NULL) return ARC_ERR_BAD_ARG;
    if (msg->slot_count > ARC_FC_VIDEO_STATUS_MAX_SLOTS) return ARC_ERR_BAD_ARG;
    if (msg->sender_count > ARC_FC_VIDEO_STATUS_MAX_SENDERS) return ARC_ERR_BAD_ARG;

    size_t needed = ARC_FC_VIDEO_STATUS_REPORT_SIZE(msg->slot_count,
                                                    msg->sender_count);
    if (needed > ARC_MAX_PAYLOAD_SIZE) return ARC_ERR_TOO_LONG;
    if (out_capacity < needed) return ARC_ERR_BUFFER;

    size_t off = 0;
    out[off++] = msg->slot_count;
    for (uint8_t i = 0; i < msg->slot_count; i++) {
        out[off++] = msg->slots[i];
    }
    out[off++] = msg->sender_count;
    for (uint8_t i = 0; i < msg->sender_count; i++) {
        out[off++] = msg->senders[i].addr;
        out[off++] = msg->senders[i].flags;
    }
    return (int)off;
}

arc_result_t arc_fc_video_status_report_decode(const uint8_t* in, size_t len,
                                               arc_fc_video_status_report_t* msg)
{
    if (in == NULL || msg == NULL) return ARC_ERR_BAD_ARG;
    if (len < 2) return ARC_ERR_BAD_LENGTH;

    size_t off = 0;
    uint8_t slot_count = in[off++];
    if (slot_count > ARC_FC_VIDEO_STATUS_MAX_SLOTS) return ARC_ERR_TOO_LONG;
    if (len < (size_t)1 + slot_count + 1) return ARC_ERR_BAD_LENGTH;

    msg->slot_count = slot_count;
    for (uint8_t i = 0; i < slot_count; i++) {
        msg->slots[i] = in[off++];
    }

    uint8_t sender_count = in[off++];
    if (sender_count > ARC_FC_VIDEO_STATUS_MAX_SENDERS) return ARC_ERR_TOO_LONG;
    if (len != ARC_FC_VIDEO_STATUS_REPORT_SIZE(slot_count, sender_count)) {
        return ARC_ERR_BAD_LENGTH;
    }

    msg->sender_count = sender_count;
    for (uint8_t i = 0; i < sender_count; i++) {
        msg->senders[i].addr  = in[off++];
        msg->senders[i].flags = in[off++];
    }
    return ARC_OK;
}
