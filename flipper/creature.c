/*
 * Creature -- the voice of a Trill Craft touch sculpture, on a Flipper Zero.
 * A Trill Bar, Square, Ring, Hex or Flex is accepted too, as a wiring test.
 *
 * Wiring (Flipper GPIO header -> Trill):
 *   pin 9  3V3 -> VCC (+V)
 *   pin 11 GND -> GND
 *   pin 15 C1  -> SDA
 *   pin 16 C0  -> SCL
 *
 * The Flipper's external I2C bus has no pull-up resistors and neither does the
 * Trill. This app switches on the MCU's weak internal pull-ups, which is enough
 * for short wires. If the Craft is not found, add 4.7k from 3V3 to SDA and from
 * 3V3 to SCL.
 *
 * Trill protocol taken from BelaPlatform/Trill-Arduino and Trill-Linux.
 */

#include <furi.h>
#include <furi_hal.h>
#include <gui/gui.h>
#include <input/input.h>
#include <notification/notification_messages.h>
#include <math.h>

#define TAG "Creature"

#define TRILL_NUM_CH 30
#define TRILL_CRAFT  3 // device type reported by a Craft
#define I2C_TIMEOUT  20

// Trill register offsets and commands
#define OFFSET_COMMAND 0
#define OFFSET_DATA    4
#define CMD_MODE            1
#define CMD_SCAN_SETTINGS   2
#define CMD_PRESCALER       3
#define CMD_NOISE_THRESHOLD 4
#define CMD_BASELINE_UPDATE 6
#define CMD_SCAN_TRIGGER    15 // firmware 3+
#define CMD_ACK             254
#define CMD_IDENTIFY        255
#define MODE_DIFF 3
#define SCAN_TRIGGER_DISABLED 0
#define SCAN_TRIGGER_I2C      1
#define SCAN_BITS 12

#define POLL_MS       10
#define ARP_MS        55 // several limbs held: step through them this fast
#define VIBRO_MS      25
#define SEARCH_MS     1000
#define MAX_READ_FAIL 8
#define ON_FRAMES     2 // debounce
#define OFF_FRAMES    3
#define ROOT_MIDI     72 // C5; the piezo is weak below this
#define OCTAVE_SPAN   3 // pads beyond three octaves wrap around

typedef struct {
    const char* name;
    uint8_t len;
    uint8_t steps[12];
} Scale;

static const Scale scales[] = {
    {"Pentatonic", 5, {0, 2, 4, 7, 9}},
    {"Minor penta", 5, {0, 3, 5, 7, 10}},
    {"Major", 7, {0, 2, 4, 5, 7, 9, 11}},
    {"Harmonic minor", 7, {0, 2, 3, 5, 7, 8, 11}},
    {"Whole tone", 6, {0, 2, 4, 6, 8, 10}},
    {"Chromatic", 12, {0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11}},
};
#define NUM_SCALES COUNT_OF(scales)

// Touch threshold per sensitivity step, in 12-bit sensor units. Higher step = lighter touch.
static const uint16_t sens_threshold[] = {800, 560, 400, 280, 200, 140, 100, 70};
#define NUM_SENS     COUNT_OF(sens_threshold)
#define DEFAULT_SENS 3

static const char* const note_names[12] =
    {"C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"};

// LED colour per scale degree (r, g, b)
static const uint8_t degree_rgb[][3] = {
    {255, 0, 0},
    {255, 120, 0},
    {200, 255, 0},
    {0, 255, 0},
    {0, 255, 200},
    {0, 80, 255},
    {180, 0, 255},
};

// Every Trill sensor answers at its own default address. The Craft is the
// sculpture; the others are accepted so the wiring can be tested with a Bar or
// Square, which need no soldering.
typedef struct {
    uint8_t type;
    uint8_t addr; // 7-bit
    uint8_t channels;
    uint8_t prescaler; // Trill-Linux default
    const char* name;
} TrillKind;

static const TrillKind trill_kinds[] = {
    {3, 0x30, 30, 1, "Craft"},
    {1, 0x20, 26, 2, "Bar"},
    {2, 0x28, 30, 1, "Square"},
    {4, 0x38, 30, 2, "Ring"},
    {5, 0x40, 30, 1, "Hex"},
    {6, 0x48, 30, 4, "Flex"},
};

static uint8_t trill_addr8 = 0x30 << 1; // address in use, 8-bit form

typedef enum {
    StateSearching,
    StatePlaying,
} State;

typedef struct {
    FuriMutex* mutex; // guards everything the draw callback reads
    FuriMessageQueue* input_queue;
    ViewPort* view_port;
    Gui* gui;
    NotificationApp* notif;
    bool have_speaker;

    State state;
    uint8_t dev_type;
    const TrillKind* kind; // NULL until a sensor answers
    uint8_t fw;
    uint8_t prescaler; // 1..8; larger electrodes need a higher value
    uint8_t sens;
    uint8_t scale;

    uint16_t level[TRILL_NUM_CH];
    uint16_t noise[TRILL_NUM_CH];
    uint16_t on_thr[TRILL_NUM_CH];
    int8_t debounce[TRILL_NUM_CH];
    bool on[TRILL_NUM_CH];

    int playing_ch; // -1 = silent
    uint8_t arp_idx;
    uint32_t arp_tick;
    float volume;
    uint32_t vibro_off_tick; // 0 = motor idle
    uint8_t read_fail;
    uint8_t search_count;
} App;

/* ---------- sound helpers ---------- */

static int channel_midi(const App* app, int ch) {
    const Scale* s = &scales[app->scale];
    int d = ch % (s->len * OCTAVE_SPAN);
    return ROOT_MIDI + 12 * (d / s->len) + s->steps[d % s->len];
}

static float midi_freq(int midi) {
    return 440.0f * powf(2.0f, (float)(midi - 69) / 12.0f);
}

static void voice_stop(App* app) {
    if(app->have_speaker && app->playing_ch >= 0) furi_hal_speaker_stop();
    app->playing_ch = -1;
}

// Blocking beep for status sounds, so nothing depends on reading the screen.
static void beep(App* app, float freq, uint32_t ms, float volume) {
    if(!app->have_speaker) {
        furi_delay_ms(ms);
        return;
    }
    furi_hal_speaker_start(freq, volume);
    furi_delay_ms(ms);
    furi_hal_speaker_stop();
}

static void led_set(uint8_t r, uint8_t g, uint8_t b) {
    furi_hal_light_set(LightRed, r);
    furi_hal_light_set(LightGreen, g);
    furi_hal_light_set(LightBlue, b);
}

/* ---------- Trill over I2C ---------- */

static bool trill_tx(const uint8_t* data, size_t size) {
    return furi_hal_i2c_tx(&furi_hal_i2c_handle_external, trill_addr8, data, size, I2C_TIMEOUT);
}

static bool trill_rx(uint8_t* data, size_t size) {
    return furi_hal_i2c_rx(&furi_hal_i2c_handle_external, trill_addr8, data, size, I2C_TIMEOUT);
}

// Send a command and give the sensor time to take it. Firmware 3+ acknowledges
// commands; older firmware just needs a pause. A missing ack is not fatal.
static bool trill_cmd(App* app, const uint8_t* payload, size_t size) {
    uint8_t buf[4] = {OFFSET_COMMAND, 0, 0, 0};
    furi_assert(size <= 3);
    memcpy(&buf[1], payload, size);
    if(!trill_tx(buf, size + 1)) return false;

    if(app->fw < 3) {
        furi_delay_ms(15);
        return true;
    }
    uint32_t wait = 1;
    uint32_t total = 0;
    while(total < 200) {
        furi_delay_ms(wait);
        uint8_t ack = 0;
        if(!trill_rx(&ack, 1)) return false;
        if(ack == CMD_ACK) return true;
        total += wait;
        wait *= 2;
    }
    FURI_LOG_W(TAG, "no ack for command %u", payload[0]);
    return true;
}

static bool trill_point_at_data(void) {
    const uint8_t offset = OFFSET_DATA;
    bool ok = trill_tx(&offset, 1);
    furi_delay_ms(2);
    return ok;
}

static bool trill_read_frame(uint16_t* out) {
    uint8_t raw[TRILL_NUM_CH * 2];
    if(!trill_rx(raw, sizeof(raw))) return false;
    for(size_t i = 0; i < TRILL_NUM_CH; i++) {
        out[i] = ((uint16_t)raw[2 * i] << 8) | raw[2 * i + 1];
    }
    return true;
}

static void update_thresholds(App* app) {
    uint16_t base = sens_threshold[app->sens];
    for(size_t i = 0; i < TRILL_NUM_CH; i++) {
        uint16_t floor = app->noise[i] * 3 + 20;
        app->on_thr[i] = MAX(base, floor);
    }
}

// Take a fresh "nobody is touching" baseline, then measure the resting noise on
// each pad so a noisy wire cannot trigger itself. Hands off while this runs.
static bool trill_calibrate(App* app) {
    const uint8_t baseline[] = {CMD_BASELINE_UPDATE};
    if(!trill_cmd(app, baseline, sizeof(baseline))) return false;
    if(!trill_point_at_data()) return false;
    furi_delay_ms(60);

    uint16_t noise[TRILL_NUM_CH] = {0};
    uint16_t frame[TRILL_NUM_CH];
    for(int n = 0; n < 25; n++) {
        if(!trill_read_frame(frame)) return false;
        for(size_t i = 0; i < TRILL_NUM_CH; i++) noise[i] = MAX(noise[i], frame[i]);
        furi_delay_ms(POLL_MS);
    }

    furi_mutex_acquire(app->mutex, FuriWaitForever);
    memcpy(app->noise, noise, sizeof(noise));
    memset(app->level, 0, sizeof(app->level));
    memset(app->debounce, 0, sizeof(app->debounce));
    memset(app->on, 0, sizeof(app->on));
    update_thresholds(app);
    furi_mutex_release(app->mutex);
    return true;
}

// Ask one address who is there. Written before the firmware version is known,
// so it uses a plain delay rather than waiting for an ack.
static bool trill_identify(App* app, const TrillKind* kind) {
    trill_addr8 = kind->addr << 1;
    const uint8_t identify[] = {OFFSET_COMMAND, CMD_IDENTIFY};
    if(!trill_tx(identify, sizeof(identify))) return false;
    furi_delay_ms(25);
    uint8_t id[3] = {0};
    if(!trill_rx(id, sizeof(id))) return false;
    if(id[1] == 0) return false;
    if(app->kind != kind) app->prescaler = kind->prescaler; // new sensor: its own default
    app->dev_type = id[1];
    app->kind = kind;
    app->fw = id[2];
    FURI_LOG_I(TAG, "Trill %s type %u firmware %u", kind->name, app->dev_type, app->fw);
    return true;
}

static bool trill_init(App* app) {
    bool found = false;
    // The sensor already in use is tried first, so a hold-OK resize keeps it.
    if(app->kind) found = trill_identify(app, app->kind);
    for(size_t k = 0; !found && k < COUNT_OF(trill_kinds); k++) {
        found = trill_identify(app, &trill_kinds[k]);
    }
    if(!found) return false;

    const uint8_t scan_off[] = {CMD_SCAN_TRIGGER, SCAN_TRIGGER_DISABLED};
    const uint8_t scan_i2c[] = {CMD_SCAN_TRIGGER, SCAN_TRIGGER_I2C};
    const uint8_t mode[] = {CMD_MODE, MODE_DIFF};
    const uint8_t prescaler[] = {CMD_PRESCALER, app->prescaler};
    const uint8_t scan[] = {CMD_SCAN_SETTINGS, 0, SCAN_BITS};
    const uint8_t noise[] = {CMD_NOISE_THRESHOLD, 0x28};

    if(app->fw >= 3 && !trill_cmd(app, scan_off, sizeof(scan_off))) return false;
    if(!trill_cmd(app, mode, sizeof(mode))) return false;
    if(!trill_cmd(app, prescaler, sizeof(prescaler))) return false;
    if(!trill_cmd(app, scan, sizeof(scan))) return false;
    if(!trill_cmd(app, noise, sizeof(noise))) return false;
    if(app->fw >= 3 && !trill_cmd(app, scan_i2c, sizeof(scan_i2c))) return false;
    return trill_calibrate(app);
}

/* ---------- state changes ---------- */

static void enter_playing(App* app) {
    furi_mutex_acquire(app->mutex, FuriWaitForever);
    app->state = StatePlaying;
    app->read_fail = 0;
    furi_mutex_release(app->mutex);
    // rising: found it
    beep(app, midi_freq(72), 70, 0.6f);
    beep(app, midi_freq(76), 70, 0.6f);
    beep(app, midi_freq(79), 110, 0.6f);
}

static void enter_searching(App* app) {
    voice_stop(app);
    led_set(0, 0, 0);
    furi_mutex_acquire(app->mutex, FuriWaitForever);
    app->state = StateSearching;
    app->search_count = 0;
    memset(app->level, 0, sizeof(app->level));
    memset(app->on, 0, sizeof(app->on));
    furi_mutex_release(app->mutex);
    // falling: lost it
    beep(app, midi_freq(76), 90, 0.6f);
    beep(app, midi_freq(69), 140, 0.6f);
}

static void search_step(App* app) {
    if(trill_init(app)) {
        enter_playing(app);
        return;
    }
    // quiet heartbeat every other attempt: alive, but no Craft on the wires
    if((app->search_count++ & 1) == 0) beep(app, 330.0f, 35, 0.25f);
}

static void play_step(App* app) {
    uint16_t frame[TRILL_NUM_CH];
    if(!trill_read_frame(frame)) {
        if(++app->read_fail >= MAX_READ_FAIL) enter_searching(app);
        return;
    }
    app->read_fail = 0;

    // A Bar has fewer channels than the 30 read. On any slider sensor one finger
    // covers several neighbouring channels, so only the strongest one counts.
    uint8_t channels = app->kind ? app->kind->channels : TRILL_NUM_CH;
    for(size_t i = channels; i < TRILL_NUM_CH; i++) frame[i] = 0;
    int strongest = -1;
    if(app->dev_type != TRILL_CRAFT) {
        strongest = 0;
        for(size_t i = 1; i < channels; i++) {
            if(frame[i] > frame[strongest]) strongest = i;
        }
    }

    uint8_t touched[TRILL_NUM_CH];
    uint8_t count = 0;
    bool new_touch = false;

    furi_mutex_acquire(app->mutex, FuriWaitForever);
    for(size_t i = 0; i < TRILL_NUM_CH; i++) {
        app->level[i] = frame[i];
        if(strongest >= 0 && (int)i != strongest) frame[i] = 0;
        uint16_t off_thr = app->on_thr[i] * 3 / 5;
        if(!app->on[i]) {
            app->debounce[i] = frame[i] > app->on_thr[i] ? app->debounce[i] + 1 : 0;
            if(app->debounce[i] >= ON_FRAMES) {
                app->on[i] = true;
                app->debounce[i] = 0;
                new_touch = true;
            }
        } else {
            app->debounce[i] = frame[i] < off_thr ? app->debounce[i] + 1 : 0;
            if(app->debounce[i] >= OFF_FRAMES) {
                app->on[i] = false;
                app->debounce[i] = 0;
            }
        }
        if(app->on[i]) touched[count++] = i;
    }
    furi_mutex_release(app->mutex);

    uint32_t now = furi_get_tick();
    if(new_touch) {
        furi_hal_vibro_on(true);
        app->vibro_off_tick = now + furi_ms_to_ticks(VIBRO_MS);
        if(app->vibro_off_tick == 0) app->vibro_off_tick = 1;
    }

    if(count == 0) {
        if(app->playing_ch >= 0) {
            voice_stop(app);
            led_set(0, 0, 0);
        }
        return;
    }

    // One limb sings. Several limbs take turns quickly, which reads as a chord.
    if(count == 1) {
        app->arp_idx = 0;
    } else if(now - app->arp_tick >= furi_ms_to_ticks(ARP_MS)) {
        app->arp_idx++;
        app->arp_tick = now;
    }
    int target = touched[app->arp_idx % count];

    // A firmer grip is louder.
    float thr = (float)app->on_thr[target];
    float x = ((float)frame[target] - thr) / (3.0f * thr);
    x = CLAMP(x, 1.0f, 0.0f);
    float volume = 0.35f + 0.65f * x;
    app->volume += 0.3f * (volume - app->volume);

    if(target != app->playing_ch) {
        int midi = channel_midi(app, target);
        if(app->have_speaker) furi_hal_speaker_start(midi_freq(midi), app->volume);
        const uint8_t* rgb =
            degree_rgb[(target % scales[app->scale].len) % COUNT_OF(degree_rgb)];
        led_set(rgb[0], rgb[1], rgb[2]);
        furi_mutex_acquire(app->mutex, FuriWaitForever);
        app->playing_ch = target;
        furi_mutex_release(app->mutex);
    } else if(app->have_speaker) {
        furi_hal_speaker_set_volume(app->volume);
    }
}

/* ---------- buttons ---------- */

static void recalibrate(App* app) {
    voice_stop(app);
    led_set(0, 0, 0);
    beep(app, 660.0f, 60, 0.5f); // hands off now
    if(app->state == StatePlaying && trill_calibrate(app)) {
        beep(app, 880.0f, 50, 0.5f);
        furi_delay_ms(40);
        beep(app, 880.0f, 50, 0.5f);
    } else if(app->state == StatePlaying) {
        enter_searching(app);
    }
}

static void next_prescaler(App* app) {
    voice_stop(app);
    furi_mutex_acquire(app->mutex, FuriWaitForever);
    app->prescaler = app->prescaler % 8 + 1;
    furi_mutex_release(app->mutex);
    // count it out: one beep per prescaler step
    for(uint8_t n = 0; n < app->prescaler; n++) {
        beep(app, 1200.0f, 35, 0.5f);
        furi_delay_ms(55);
    }
    if(app->state == StatePlaying && !trill_init(app)) enter_searching(app);
}

static void change_sens(App* app, int delta) {
    voice_stop(app);
    int sens = CLAMP((int)app->sens + delta, (int)NUM_SENS - 1, 0);
    furi_mutex_acquire(app->mutex, FuriWaitForever);
    app->sens = sens;
    update_thresholds(app);
    furi_mutex_release(app->mutex);
    beep(app, 500.0f + 150.0f * sens, 45, 0.5f); // higher pitch = lighter touch
}

static void change_scale(App* app, int delta) {
    voice_stop(app);
    furi_mutex_acquire(app->mutex, FuriWaitForever);
    app->scale = (app->scale + NUM_SCALES + delta) % NUM_SCALES;
    furi_mutex_release(app->mutex);
    // play the new scale's first octave so it can be told apart by ear
    uint8_t len = MIN(scales[app->scale].len, 7);
    for(uint8_t n = 0; n < len; n++) beep(app, midi_freq(channel_midi(app, n)), 45, 0.5f);
}

// Returns false when the app should exit.
static bool handle_input(App* app, const InputEvent* event) {
    if(event->key == InputKeyBack) {
        return !(event->type == InputTypeShort || event->type == InputTypeLong);
    }
    if(event->key == InputKeyOk) {
        if(event->type == InputTypeShort) recalibrate(app);
        if(event->type == InputTypeLong) next_prescaler(app);
        return true;
    }
    if(event->type != InputTypeShort && event->type != InputTypeRepeat) return true;
    switch(event->key) {
    case InputKeyUp:
        change_sens(app, 1);
        break;
    case InputKeyDown:
        change_sens(app, -1);
        break;
    case InputKeyRight:
        change_scale(app, 1);
        break;
    case InputKeyLeft:
        change_scale(app, -1);
        break;
    default:
        break;
    }
    return true;
}

/* ---------- screen ---------- */

#define BAR_X      4
#define BAR_PITCH  4
#define BAR_W      3
#define BAR_BASE   52
#define BAR_MAX_H  36
#define BAR_THR_H  9 // a level exactly at the threshold draws this tall

static void draw_callback(Canvas* canvas, void* ctx) {
    App* app = ctx;
    if(furi_mutex_acquire(app->mutex, 25) != FuriStatusOk) return;

    canvas_clear(canvas);
    canvas_set_font(canvas, FontPrimary);

    if(app->state == StateSearching) {
        canvas_draw_str(canvas, 2, 12, "No Trill found");
        canvas_set_font(canvas, FontSecondary);
        canvas_draw_str(canvas, 2, 27, "3V3 pin 9     GND pin 11");
        canvas_draw_str(canvas, 2, 39, "SDA pin 15   SCL pin 16");
        canvas_draw_str(canvas, 2, 51, "Still nothing? Add 4.7k");
        canvas_draw_str(canvas, 2, 62, "pull-ups to SDA and SCL");
        furi_mutex_release(app->mutex);
        return;
    }

    canvas_draw_str(canvas, 2, 10, scales[app->scale].name);
    if(app->playing_ch >= 0) {
        int midi = channel_midi(app, app->playing_ch);
        char note[16];
        snprintf(note, sizeof(note), "%s%d", note_names[midi % 12], midi / 12 - 1);
        canvas_draw_str_aligned(canvas, 126, 10, AlignRight, AlignBottom, note);
    }

    // One bar per pad. Solid = touching. The dotted line is the touch threshold.
    for(int i = 0; i < TRILL_NUM_CH; i++) {
        uint32_t h = (uint32_t)app->level[i] * BAR_THR_H / app->on_thr[i];
        h = CLAMP(h, (uint32_t)BAR_MAX_H, (uint32_t)1);
        int x = BAR_X + i * BAR_PITCH;
        if(app->on[i] || h < 3) {
            canvas_draw_box(canvas, x, BAR_BASE - h, BAR_W, h);
        } else {
            canvas_draw_frame(canvas, x, BAR_BASE - h, BAR_W, h);
        }
    }
    for(int x = 0; x < 128; x += 4) canvas_draw_dot(canvas, x, BAR_BASE - BAR_THR_H);

    canvas_set_font(canvas, FontSecondary);
    char status[40];
    snprintf(
        status,
        sizeof(status),
        "touch %u/%u   size %u   %s",
        app->sens + 1,
        (unsigned)NUM_SENS,
        app->prescaler,
        app->kind ? app->kind->name : "?");
    canvas_draw_str(canvas, 2, 63, status);

    furi_mutex_release(app->mutex);
}

static void input_callback(InputEvent* event, void* ctx) {
    App* app = ctx;
    furi_message_queue_put(app->input_queue, event, 0);
}

/* ---------- entry point ---------- */

int32_t creature_app(void* p) {
    UNUSED(p);
    App* app = malloc(sizeof(App));
    memset(app, 0, sizeof(App));
    app->mutex = furi_mutex_alloc(FuriMutexTypeNormal);
    app->input_queue = furi_message_queue_alloc(8, sizeof(InputEvent));
    app->state = StateSearching;
    app->prescaler = 1; // Trill-Linux default for the Craft
    app->sens = DEFAULT_SENS;
    app->playing_ch = -1;
    app->volume = 0.5f;
    for(size_t i = 0; i < TRILL_NUM_CH; i++) app->on_thr[i] = sens_threshold[DEFAULT_SENS];

    app->view_port = view_port_alloc();
    view_port_draw_callback_set(app->view_port, draw_callback, app);
    view_port_input_callback_set(app->view_port, input_callback, app);
    app->gui = furi_record_open(RECORD_GUI);
    gui_add_view_port(app->gui, app->view_port, GuiLayerFullscreen);
    app->notif = furi_record_open(RECORD_NOTIFICATION);
    notification_message(app->notif, &sequence_display_backlight_enforce_on);

    app->have_speaker = furi_hal_speaker_acquire(1000);

    // Hold the external I2C bus for the whole run and add the internal pull-ups,
    // which the firmware leaves off.
    furi_hal_i2c_acquire(&furi_hal_i2c_handle_external);
    furi_hal_gpio_init_ex(
        &gpio_ext_pc0, GpioModeAltFunctionOpenDrain, GpioPullUp, GpioSpeedLow, GpioAltFn4I2C3);
    furi_hal_gpio_init_ex(
        &gpio_ext_pc1, GpioModeAltFunctionOpenDrain, GpioPullUp, GpioSpeedLow, GpioAltFn4I2C3);

    uint32_t last_poll = 0;
    uint32_t last_search = 0;
    bool searched_once = false;
    bool running = true;
    while(running) {
        InputEvent event;
        if(furi_message_queue_get(app->input_queue, &event, POLL_MS) == FuriStatusOk) {
            running = handle_input(app, &event);
        }

        uint32_t now = furi_get_tick();
        if(app->vibro_off_tick && (int32_t)(now - app->vibro_off_tick) >= 0) {
            furi_hal_vibro_on(false);
            app->vibro_off_tick = 0;
        }

        if(app->state == StateSearching) {
            if(!searched_once || now - last_search >= furi_ms_to_ticks(SEARCH_MS)) {
                searched_once = true;
                search_step(app);
                last_search = furi_get_tick();
            }
        } else if(now - last_poll >= furi_ms_to_ticks(POLL_MS)) {
            last_poll = now;
            play_step(app);
        }
        view_port_update(app->view_port);
    }

    voice_stop(app);
    furi_hal_vibro_on(false);
    furi_hal_i2c_release(&furi_hal_i2c_handle_external);
    if(app->have_speaker) furi_hal_speaker_release();
    notification_message(app->notif, &sequence_reset_rgb);
    notification_message(app->notif, &sequence_display_backlight_enforce_auto);

    gui_remove_view_port(app->gui, app->view_port);
    view_port_free(app->view_port);
    furi_record_close(RECORD_GUI);
    furi_record_close(RECORD_NOTIFICATION);
    furi_message_queue_free(app->input_queue);
    furi_mutex_free(app->mutex);
    free(app);
    return 0;
}
