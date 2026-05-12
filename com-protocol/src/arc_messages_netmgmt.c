// arc_messages_netmgmt.c
// Encoder/decoder for the NETMGMT family ACK payload.

#include "arc_messages_netmgmt.h"

int arc_netmgmt_ack_encode(const arc_netmgmt_ack_t* msg,
                           uint8_t* out, size_t out_capacity)
{
    if (msg == NULL || out == NULL) return ARC_ERR_BAD_ARG;
    if (out_capacity < ARC_NETMGMT_ACK_PAYLOAD_SIZE) return ARC_ERR_BUFFER;
    out[0] = (uint8_t)(msg->seq >> 8);
    out[1] = (uint8_t)(msg->seq & 0xFF);
    return ARC_NETMGMT_ACK_PAYLOAD_SIZE;
}

arc_result_t arc_netmgmt_ack_decode(const uint8_t* in, size_t len,
                                    arc_netmgmt_ack_t* msg)
{
    if (in == NULL || msg == NULL) return ARC_ERR_BAD_ARG;
    if (len != ARC_NETMGMT_ACK_PAYLOAD_SIZE) return ARC_ERR_BAD_LENGTH;
    msg->seq = ((uint16_t)in[0] << 8) | (uint16_t)in[1];
    return ARC_OK;
}
