// test_arc_messages.c
// Round-trip tests for the per-family payload helpers.

#include "../src/arc_messages_netmgmt.h"
#include "../src/arc_messages_video.h"
#include "../src/arc_messages_fc_video.h"
#include "../src/arc_messages_radio.h"
#include "../src/arc_messages_power.h"

#include <stdio.h>
#include <string.h>

static int tests_run = 0;
static int tests_failed = 0;

#define TEST(name) static void test_##name(void)
#define RUN(name) do { \
    printf("  %-44s ", #name); fflush(stdout); \
    tests_run++; \
    int failed_before = tests_failed; \
    test_##name(); \
    if (tests_failed == failed_before) printf("OK\n"); \
} while (0)

#define ASSERT(cond) do { \
    if (!(cond)) { \
        printf("FAIL\n    %s:%d: %s\n", __FILE__, __LINE__, #cond); \
        tests_failed++; \
        return; \
    } \
} while (0)

#define ASSERT_EQ(a, b) do { \
    long _a = (long)(a); long _b = (long)(b); \
    if (_a != _b) { \
        printf("FAIL\n    %s:%d: expected %ld, got %ld\n", \
               __FILE__, __LINE__, _b, _a); \
        tests_failed++; \
        return; \
    } \
} while (0)

#define ASSERT_BYTES_EQ(actual, expected, n) do { \
    if (memcmp((actual), (expected), (n)) != 0) { \
        printf("FAIL\n    %s:%d: byte mismatch over %zu bytes\n", \
               __FILE__, __LINE__, (size_t)(n)); \
        tests_failed++; \
        return; \
    } \
} while (0)

// ----------------------------------------------------------------------
// NETMGMT ACK
// ----------------------------------------------------------------------

TEST(netmgmt_ack_roundtrip)
{
    arc_netmgmt_ack_t in = { .seq = 0x1234 };
    uint8_t buf[8];
    int n = arc_netmgmt_ack_encode(&in, buf, sizeof(buf));
    ASSERT_EQ(n, 2);
    const uint8_t expected[] = {0x12, 0x34};
    ASSERT_BYTES_EQ(buf, expected, 2);

    arc_netmgmt_ack_t out;
    ASSERT_EQ(arc_netmgmt_ack_decode(buf, n, &out), ARC_OK);
    ASSERT_EQ(out.seq, 0x1234);
}

TEST(netmgmt_ack_buffer_too_small)
{
    arc_netmgmt_ack_t msg = { .seq = 1 };
    uint8_t buf[1];
    ASSERT_EQ(arc_netmgmt_ack_encode(&msg, buf, sizeof(buf)), ARC_ERR_BUFFER);
}

TEST(netmgmt_ack_decode_bad_length)
{
    const uint8_t buf[3] = {0, 0, 0};
    arc_netmgmt_ack_t out;
    ASSERT_EQ(arc_netmgmt_ack_decode(buf, 3, &out), ARC_ERR_BAD_LENGTH);
    ASSERT_EQ(arc_netmgmt_ack_decode(buf, 1, &out), ARC_ERR_BAD_LENGTH);
}

// ----------------------------------------------------------------------
// VIDEO SET_BITRATE
// ----------------------------------------------------------------------

TEST(video_set_bitrate_roundtrip)
{
    arc_video_set_bitrate_t in = { .bitrate_bps = 2500000 };
    uint8_t buf[8];
    int n = arc_video_set_bitrate_encode(&in, buf, sizeof(buf));
    ASSERT_EQ(n, 4);
    // 2500000 = 0x0026_25A0
    const uint8_t expected[] = {0x00, 0x26, 0x25, 0xA0};
    ASSERT_BYTES_EQ(buf, expected, 4);

    arc_video_set_bitrate_t out;
    ASSERT_EQ(arc_video_set_bitrate_decode(buf, n, &out), ARC_OK);
    ASSERT_EQ(out.bitrate_bps, 2500000);
}

TEST(video_set_bitrate_max)
{
    arc_video_set_bitrate_t in = { .bitrate_bps = 0xFFFFFFFFu };
    uint8_t buf[4];
    ASSERT_EQ(arc_video_set_bitrate_encode(&in, buf, sizeof(buf)), 4);
    arc_video_set_bitrate_t out;
    ASSERT_EQ(arc_video_set_bitrate_decode(buf, 4, &out), ARC_OK);
    ASSERT_EQ(out.bitrate_bps, 0xFFFFFFFFu);
}

// ----------------------------------------------------------------------
// VIDEO STATUS_REPORT
// ----------------------------------------------------------------------

TEST(video_status_report_roundtrip)
{
    arc_video_status_report_t in = {
        .state          = 0x03,
        .cpu_temp_c     = 47,
        .cpu_load_pct   = 23,
        .free_disk_mb   = 4096,
        .rssi_dbm       = -58,
        .tx_frames      = 120,
        .dropped_frames = 2,
    };
    uint8_t buf[16];
    int n = arc_video_status_report_encode(&in, buf, sizeof(buf));
    ASSERT_EQ(n, 10);

    // Verify exact byte layout (mirrors Python StatusReport.encode()).
    // free_disk_mb=4096 -> 0x10 0x00; rssi -58 -> 0xC6;
    // tx_frames=120 -> 0x00 0x78; dropped=2 -> 0x00 0x02
    const uint8_t expected[] = {
        0x03, 0x2F, 0x17, 0x10, 0x00, 0xC6, 0x00, 0x78, 0x00, 0x02
    };
    ASSERT_BYTES_EQ(buf, expected, 10);

    arc_video_status_report_t out;
    ASSERT_EQ(arc_video_status_report_decode(buf, n, &out), ARC_OK);
    ASSERT_EQ(out.state, 0x03);
    ASSERT_EQ(out.cpu_temp_c, 47);
    ASSERT_EQ(out.cpu_load_pct, 23);
    ASSERT_EQ(out.free_disk_mb, 4096);
    ASSERT_EQ(out.rssi_dbm, -58);
    ASSERT_EQ(out.tx_frames, 120);
    ASSERT_EQ(out.dropped_frames, 2);
}

TEST(video_status_report_negative_rssi_extreme)
{
    arc_video_status_report_t in = {0};
    in.rssi_dbm = -128;
    uint8_t buf[10];
    ASSERT_EQ(arc_video_status_report_encode(&in, buf, sizeof(buf)), 10);
    ASSERT_EQ(buf[5], 0x80);

    arc_video_status_report_t out;
    ASSERT_EQ(arc_video_status_report_decode(buf, 10, &out), ARC_OK);
    ASSERT_EQ(out.rssi_dbm, -128);
}

// ----------------------------------------------------------------------
// FC_VIDEO SET_LAYOUT / SET_SOURCE
// ----------------------------------------------------------------------

TEST(fc_video_set_layout_roundtrip)
{
    arc_fc_video_set_layout_t in = { .layout_id = 2 };
    uint8_t buf[4];
    int n = arc_fc_video_set_layout_encode(&in, buf, sizeof(buf));
    ASSERT_EQ(n, 1);
    ASSERT_EQ(buf[0], 2);

    arc_fc_video_set_layout_t out;
    ASSERT_EQ(arc_fc_video_set_layout_decode(buf, n, &out), ARC_OK);
    ASSERT_EQ(out.layout_id, 2);
}

TEST(fc_video_set_source_roundtrip)
{
    arc_fc_video_set_source_t in = { .slot_id = 1, .sender_addr = ARC_ADDR_SENDER_AIRBRAKE };
    uint8_t buf[4];
    int n = arc_fc_video_set_source_encode(&in, buf, sizeof(buf));
    ASSERT_EQ(n, 2);
    ASSERT_EQ(buf[0], 1);
    ASSERT_EQ(buf[1], ARC_ADDR_SENDER_AIRBRAKE);

    arc_fc_video_set_source_t out;
    ASSERT_EQ(arc_fc_video_set_source_decode(buf, n, &out), ARC_OK);
    ASSERT_EQ(out.slot_id, 1);
    ASSERT_EQ(out.sender_addr, ARC_ADDR_SENDER_AIRBRAKE);
}

// ----------------------------------------------------------------------
// FC_VIDEO SET_OVERLAY
// ----------------------------------------------------------------------

TEST(fc_video_set_overlay_roundtrip)
{
    const char* text = "KD3BBP";
    uint8_t buf[16];
    int n = arc_fc_video_set_overlay_encode(text, 6, buf, sizeof(buf));
    ASSERT_EQ(n, 7);
    ASSERT_EQ(buf[6], 0);
    ASSERT_BYTES_EQ(buf, "KD3BBP\0", 7);

    char out[16];
    int len = arc_fc_video_set_overlay_decode(buf, n, out, sizeof(out));
    ASSERT_EQ(len, 6);
    ASSERT_EQ(strcmp(out, "KD3BBP"), 0);
}

TEST(fc_video_set_overlay_empty_string)
{
    uint8_t buf[2];
    int n = arc_fc_video_set_overlay_encode("", 0, buf, sizeof(buf));
    ASSERT_EQ(n, 1);
    ASSERT_EQ(buf[0], 0);

    char out[4];
    int len = arc_fc_video_set_overlay_decode(buf, n, out, sizeof(out));
    ASSERT_EQ(len, 0);
    ASSERT_EQ(out[0], 0);
}

TEST(fc_video_set_overlay_decode_unterminated)
{
    const uint8_t buf[3] = {'a', 'b', 'c'};  // no NUL terminator
    char out[4];
    ASSERT_EQ(arc_fc_video_set_overlay_decode(buf, 3, out, sizeof(out)),
              ARC_ERR_BAD_LENGTH);
}

// ----------------------------------------------------------------------
// FC_VIDEO STATUS_REPORT
// ----------------------------------------------------------------------

TEST(fc_video_status_report_roundtrip)
{
    arc_fc_video_status_report_t in = {0};
    in.slot_count = 2;
    in.slots[0] = ARC_ADDR_CONTROLLER;
    in.slots[1] = ARC_ADDR_SENDER_AIRBRAKE;
    in.sender_count = 3;
    in.senders[0] = (arc_fc_video_sender_status_t){ARC_ADDR_SENDER_DOWN, 0x01};
    in.senders[1] = (arc_fc_video_sender_status_t){ARC_ADDR_SENDER_AIRBRAKE,
        ARC_FC_VIDEO_STATUS_FLAG_ONLINE | ARC_FC_VIDEO_STATUS_FLAG_TRANSMITTING |
        ARC_FC_VIDEO_STATUS_FLAG_RECORDING};
    in.senders[2] = (arc_fc_video_sender_status_t){ARC_ADDR_SENDER_PAYLOAD, 0x00};

    uint8_t buf[32];
    int n = arc_fc_video_status_report_encode(&in, buf, sizeof(buf));
    // 1 (slot_count) + 2 (slots) + 1 (sender_count) + 6 (3 * 2) = 10
    ASSERT_EQ(n, 10);
    const uint8_t expected[] = {
        2,
        ARC_ADDR_CONTROLLER, ARC_ADDR_SENDER_AIRBRAKE,
        3,
        ARC_ADDR_SENDER_DOWN, 0x01,
        ARC_ADDR_SENDER_AIRBRAKE, 0x07,
        ARC_ADDR_SENDER_PAYLOAD, 0x00,
    };
    ASSERT_BYTES_EQ(buf, expected, 10);

    arc_fc_video_status_report_t out;
    ASSERT_EQ(arc_fc_video_status_report_decode(buf, n, &out), ARC_OK);
    ASSERT_EQ(out.slot_count, 2);
    ASSERT_EQ(out.slots[0], ARC_ADDR_CONTROLLER);
    ASSERT_EQ(out.slots[1], ARC_ADDR_SENDER_AIRBRAKE);
    ASSERT_EQ(out.sender_count, 3);
    ASSERT_EQ(out.senders[1].addr, ARC_ADDR_SENDER_AIRBRAKE);
    ASSERT_EQ(out.senders[1].flags, 0x07);
}

TEST(fc_video_status_report_empty)
{
    arc_fc_video_status_report_t in = {0};
    uint8_t buf[8];
    int n = arc_fc_video_status_report_encode(&in, buf, sizeof(buf));
    ASSERT_EQ(n, 2);
    ASSERT_EQ(buf[0], 0);
    ASSERT_EQ(buf[1], 0);

    arc_fc_video_status_report_t out;
    out.slot_count = 99;  // poison
    out.sender_count = 99;
    ASSERT_EQ(arc_fc_video_status_report_decode(buf, 2, &out), ARC_OK);
    ASSERT_EQ(out.slot_count, 0);
    ASSERT_EQ(out.sender_count, 0);
}

TEST(fc_video_status_report_truncated)
{
    // Encoded as 2 slots + 1 sender = 1 + 2 + 1 + 2 = 6 bytes; pass 5.
    const uint8_t buf[5] = {2, 0x10, 0x12, 1, 0x11};
    arc_fc_video_status_report_t out;
    ASSERT_EQ(arc_fc_video_status_report_decode(buf, 5, &out),
              ARC_ERR_BAD_LENGTH);
}

TEST(fc_video_status_report_encode_overflow)
{
    arc_fc_video_status_report_t in = {0};
    in.slot_count = ARC_FC_VIDEO_STATUS_MAX_SLOTS + 1;
    uint8_t buf[64];
    ASSERT_EQ(arc_fc_video_status_report_encode(&in, buf, sizeof(buf)),
              ARC_ERR_BAD_ARG);

    arc_fc_video_status_report_t in2 = {0};
    in2.sender_count = ARC_FC_VIDEO_STATUS_MAX_SENDERS + 1;
    ASSERT_EQ(arc_fc_video_status_report_encode(&in2, buf, sizeof(buf)),
              ARC_ERR_BAD_ARG);
}

// ----------------------------------------------------------------------
// RADIO
// ----------------------------------------------------------------------

TEST(radio_set_frequency_roundtrip)
{
    arc_radio_set_frequency_t in = { .frequency_hz = 433920000u };
    uint8_t buf[8];
    int n = arc_radio_set_frequency_encode(&in, buf, sizeof(buf));
    ASSERT_EQ(n, 4);
    // 433_920_000 = 0x19DA8400
    const uint8_t expected[] = {0x19, 0xDD, 0x18, 0x00};
    ASSERT_BYTES_EQ(buf, expected, 4);

    arc_radio_set_frequency_t out;
    ASSERT_EQ(arc_radio_set_frequency_decode(buf, n, &out), ARC_OK);
    ASSERT_EQ(out.frequency_hz, 433920000u);
}

TEST(radio_set_tx_power_roundtrip_negative)
{
    arc_radio_set_tx_power_t in = { .tx_power_dbm = -10 };
    uint8_t buf[2];
    ASSERT_EQ(arc_radio_set_tx_power_encode(&in, buf, sizeof(buf)), 1);
    ASSERT_EQ(buf[0], 0xF6);

    arc_radio_set_tx_power_t out;
    ASSERT_EQ(arc_radio_set_tx_power_decode(buf, 1, &out), ARC_OK);
    ASSERT_EQ(out.tx_power_dbm, -10);
}

TEST(radio_status_report_roundtrip)
{
    arc_radio_status_report_t in = {
        .frequency_hz = 433920000u,
        .tx_power_dbm = 17,
        .rssi_dbm     = -78,
        .snr_db       = 9,
        .error_flags  = ARC_RADIO_ERR_RX_OVERRUN,
        .packets_rx   = 1024,
        .packets_tx   = 256,
    };
    uint8_t buf[16];
    int n = arc_radio_status_report_encode(&in, buf, sizeof(buf));
    ASSERT_EQ(n, 12);

    // freq=0x19DA8400, txp=17(0x11), rssi=-78(0xB2), snr=9(0x09),
    // err=0x08, rx=1024(0x0400), tx=256(0x0100)
    const uint8_t expected[] = {
        0x19, 0xDD, 0x18, 0x00,
        0x11, 0xB2, 0x09, 0x08,
        0x04, 0x00, 0x01, 0x00,
    };
    ASSERT_BYTES_EQ(buf, expected, 12);

    arc_radio_status_report_t out;
    ASSERT_EQ(arc_radio_status_report_decode(buf, n, &out), ARC_OK);
    ASSERT_EQ(out.frequency_hz, 433920000u);
    ASSERT_EQ(out.tx_power_dbm, 17);
    ASSERT_EQ(out.rssi_dbm, -78);
    ASSERT_EQ(out.snr_db, 9);
    ASSERT_EQ(out.error_flags, ARC_RADIO_ERR_RX_OVERRUN);
    ASSERT_EQ(out.packets_rx, 1024);
    ASSERT_EQ(out.packets_tx, 256);
}

// ----------------------------------------------------------------------
// POWER
// ----------------------------------------------------------------------

TEST(power_set_output_roundtrip)
{
    arc_power_set_output_t in = { .channel = 3, .state = ARC_POWER_ON };
    uint8_t buf[4];
    int n = arc_power_set_output_encode(&in, buf, sizeof(buf));
    ASSERT_EQ(n, 2);
    ASSERT_EQ(buf[0], 3);
    ASSERT_EQ(buf[1], ARC_POWER_ON);

    arc_power_set_output_t out;
    ASSERT_EQ(arc_power_set_output_decode(buf, n, &out), ARC_OK);
    ASSERT_EQ(out.channel, 3);
    ASSERT_EQ(out.state, ARC_POWER_ON);
}

TEST(power_set_output_rejects_invalid_state)
{
    arc_power_set_output_t in = { .channel = 0, .state = 0x42 };
    uint8_t buf[2];
    ASSERT_EQ(arc_power_set_output_encode(&in, buf, sizeof(buf)), ARC_ERR_BAD_ARG);

    // Fault bits aren't valid SET_OUTPUT inputs either.
    arc_power_set_output_t in2 = {
        .channel = 0,
        .state   = ARC_POWER_ON | ARC_POWER_CHAN_FAULT_OVERCURRENT,
    };
    ASSERT_EQ(arc_power_set_output_encode(&in2, buf, sizeof(buf)), ARC_ERR_BAD_ARG);
}

TEST(power_set_output_mask_roundtrip)
{
    // Touch channels 0,2,4 (mask 0b00010101 = 0x15);
    // drive 0 and 4 ON (state 0b00010001 = 0x11).
    arc_power_set_output_mask_t in = { .enable_mask = 0x15, .state_mask = 0x11 };
    uint8_t buf[4];
    int n = arc_power_set_output_mask_encode(&in, buf, sizeof(buf));
    ASSERT_EQ(n, 2);
    ASSERT_EQ(buf[0], 0x15);
    ASSERT_EQ(buf[1], 0x11);

    arc_power_set_output_mask_t out;
    ASSERT_EQ(arc_power_set_output_mask_decode(buf, n, &out), ARC_OK);
    ASSERT_EQ(out.enable_mask, 0x15);
    ASSERT_EQ(out.state_mask, 0x11);
}

TEST(power_status_report_roundtrip_six_channels)
{
    arc_power_status_report_t in = {0};
    in.channel_count = 6;
    in.channels[0] = (arc_power_channel_status_t){ARC_POWER_ON,  500};
    in.channels[1] = (arc_power_channel_status_t){ARC_POWER_OFF, 0};
    in.channels[2] = (arc_power_channel_status_t){ARC_POWER_ON,  1500};
    in.channels[3] = (arc_power_channel_status_t){ARC_POWER_ON  | ARC_POWER_CHAN_FAULT_OVERCURRENT, 3100};
    in.channels[4] = (arc_power_channel_status_t){ARC_POWER_OFF, 0};
    in.channels[5] = (arc_power_channel_status_t){ARC_POWER_ON,  220};
    in.bus_voltage_mv = 5012;
    in.temp_c         = 41;

    uint8_t buf[32];
    int n = arc_power_status_report_encode(&in, buf, sizeof(buf));
    // 1 + 6*3 + 2 + 1 = 22
    ASSERT_EQ(n, 22);

    // First byte = channel_count
    ASSERT_EQ(buf[0], 6);
    // Channel 3: state byte includes the fault bit (0x01 | 0x40 = 0x41), current 3100 = 0x0C1C
    ASSERT_EQ(buf[1 + 3*3 + 0], 0x41);
    ASSERT_EQ(buf[1 + 3*3 + 1], 0x0C);
    ASSERT_EQ(buf[1 + 3*3 + 2], 0x1C);
    // bus_voltage_mv at offset 1+6*3 = 19: 5012 = 0x1394
    ASSERT_EQ(buf[19], 0x13);
    ASSERT_EQ(buf[20], 0x94);
    ASSERT_EQ(buf[21], 41);

    arc_power_status_report_t out;
    ASSERT_EQ(arc_power_status_report_decode(buf, n, &out), ARC_OK);
    ASSERT_EQ(out.channel_count, 6);
    ASSERT_EQ(out.channels[0].current_ma, 500);
    ASSERT_EQ(out.channels[3].state, 0x41);
    ASSERT_EQ(out.channels[3].current_ma, 3100);
    ASSERT_EQ(out.bus_voltage_mv, 5012);
    ASSERT_EQ(out.temp_c, 41);
}

TEST(power_status_report_empty)
{
    arc_power_status_report_t in = {0};
    in.bus_voltage_mv = 0;
    in.temp_c = -10;

    uint8_t buf[8];
    int n = arc_power_status_report_encode(&in, buf, sizeof(buf));
    // 1 + 0 + 2 + 1 = 4
    ASSERT_EQ(n, 4);
    ASSERT_EQ(buf[0], 0);
    ASSERT_EQ(buf[1], 0);
    ASSERT_EQ(buf[2], 0);
    ASSERT_EQ(buf[3], 0xF6);  // -10 as int8

    arc_power_status_report_t out;
    out.channel_count = 99;  // poison
    ASSERT_EQ(arc_power_status_report_decode(buf, n, &out), ARC_OK);
    ASSERT_EQ(out.channel_count, 0);
    ASSERT_EQ(out.temp_c, -10);
}

TEST(power_status_report_decode_overflow_channels)
{
    uint8_t buf[1] = { ARC_POWER_MAX_CHANNELS + 1 };
    arc_power_status_report_t out;
    ASSERT_EQ(arc_power_status_report_decode(buf, sizeof(buf), &out),
              ARC_ERR_TOO_LONG);
}

TEST(power_status_report_decode_truncated)
{
    // channel_count = 2 -> need 1 + 6 + 2 + 1 = 10 bytes; pass 9.
    uint8_t buf[9] = {2, 0x01, 0x00, 0xC8, 0x00, 0x00, 0x00, 0x13, 0x88};
    arc_power_status_report_t out;
    ASSERT_EQ(arc_power_status_report_decode(buf, sizeof(buf), &out),
              ARC_ERR_BAD_LENGTH);
}

// ----------------------------------------------------------------------
// Driver
// ----------------------------------------------------------------------

int main(void)
{
    printf("== arc_messages tests ==\n");

    RUN(netmgmt_ack_roundtrip);
    RUN(netmgmt_ack_buffer_too_small);
    RUN(netmgmt_ack_decode_bad_length);

    RUN(video_set_bitrate_roundtrip);
    RUN(video_set_bitrate_max);
    RUN(video_status_report_roundtrip);
    RUN(video_status_report_negative_rssi_extreme);

    RUN(fc_video_set_layout_roundtrip);
    RUN(fc_video_set_source_roundtrip);
    RUN(fc_video_set_overlay_roundtrip);
    RUN(fc_video_set_overlay_empty_string);
    RUN(fc_video_set_overlay_decode_unterminated);
    RUN(fc_video_status_report_roundtrip);
    RUN(fc_video_status_report_empty);
    RUN(fc_video_status_report_truncated);
    RUN(fc_video_status_report_encode_overflow);

    RUN(radio_set_frequency_roundtrip);
    RUN(radio_set_tx_power_roundtrip_negative);
    RUN(radio_status_report_roundtrip);

    RUN(power_set_output_roundtrip);
    RUN(power_set_output_rejects_invalid_state);
    RUN(power_set_output_mask_roundtrip);
    RUN(power_status_report_roundtrip_six_channels);
    RUN(power_status_report_empty);
    RUN(power_status_report_decode_overflow_channels);
    RUN(power_status_report_decode_truncated);

    printf("\n%d run, %d failed\n", tests_run, tests_failed);
    return tests_failed == 0 ? 0 : 1;
}
