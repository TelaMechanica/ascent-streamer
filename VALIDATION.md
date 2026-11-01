# Validation record — 2026-10-24

Device: Raspberry Pi Zero 2 W, 32-bit Bookworm, kernel 6.12.109+rpt-rpi-v7. Screen confirmed by purchase date and iUniker's revision-specific files as the pre-April-2025 panel.

| Check | Evidence |
|---|---|
| Console boot, SSH and H.264 decoder | Inspected live over SSH |
| Correct display routing and video rotation | User confirmed colour bars fill the landscape screen |
| 60 Hz KMS mode | Kernel DRM state reports `320x480`, 60 Hz |
| Autologin and initial service boot | Rebooted, observed `streamer` on tty1 and active services |
| Generated H.264 720p30 decode | Played through hardware V4L2 decoder to KMS |
| Actual source | H.264 High, 1920×1080, 60 fps; encoder unchanged |
| Approved live playback | User explicitly approved UDP / 200 ms / software resize after removal of timestamp-based frame limiting |
| Waiting image | User visually confirmed their supplied image |
| Real DHCP join and playback | Passed in 16.1 seconds |
| Forced main-process crash | Service restarted with a new PID and resumed playback in about 10.1 seconds |
| Forced Wi-Fi disconnect | Rejoined and resumed playback in about 10.1 seconds |
| Fallback hotspot and subsequent recovery | Passed: AP ready after 65.1 seconds including activation; resumed video 12.2 seconds after restoring credentials |
| Hotspot SSH and page | SSH banner read on 10.42.0.1; authenticated HTTP returned 200 |
| AP graphic | Confirmed the hotspot image process runs after AP activation |
| Final unattended reboot | Passed: automatic join and video in 16.1 seconds after audit start, approved preset intact, SSH active, console autologin confirmed |

Playback is frozen. Network and boot hardware audits passed, and temporary boot-recovery units were disabled afterward.

## 1.0.4 repair validation

Releases 1.0.2 and 1.0.3 are withdrawn. The console-null change caused a boot regression on this Pi. SD boot arguments were restored without altering the root partition. The replacement preserves valid serial and tty consoles and uses `fbcon=map:1`; a subsequent reboot returned SSH and the viewer. Only the dummy console was bound, and the backing framebuffer contained zero nonzero bytes. Filesystem checks remain enabled.

The original logs show discovery and actual connection attempts to `ASCENT-example` on channel 6. Association rejected with status 16 while logs referred to the previous router; firmware scan timeouts and pending AP scans were also present. The exact cause cannot be conclusively established without reproducing against the powered target. The fresh-install setup-mode default was a separate defect, now corrected.

The controller now leaves AP mode before scans, resets station mode, waits for NetworkManager readiness, rotates failed candidates, and preserves failure details. Two AP-to-station-to-home hardware cycles passed in 3.239 and 2.779 seconds. The final full automatic test reached fallback AP in 64.058 seconds including activation and returned home in 2.367 seconds. Both transitions had zero console framebuffer bytes, the correct AP splash process ran, and playback settings matched the frozen baseline. Logs: `/var/log/ascent-radio-transition-audit.json` and `/var/log/ascent-final-repair-audit.json`.

All 25 current local automated tests pass. Full joining and live playback against the reported second AP remain unverified because it is off. The previously approved playback pipeline was unchanged.

The 12 automated tests passed on both Windows/Python 3.14 and the Pi/Python 3.11. JavaScript syntax, installer shell syntax and embedded Python syntax were also checked. Hardware playback uses GStreamer 1.22. The fresh-card installer has been syntax checked; reinstalling onto a second clean SD card was not part of this session.

Initial experiments found an invalid advertised SPS followed by a valid in-band SPS. The player waits for valid parsed dimensions before handing data to the decoder. Metadata reading handles the installed GI fraction API and cannot drop stream events on an exception. Only the player opens RTSP.

ASCENT-specific conclusions after the user supplied their handoff are based on that local handoff and live measurements, not online ASCENT documentation. An idle VRX continues streaming its local UI; absence of a powered VTX is not evidence that RTSP should stall.

## WAITING FOR VTX state ? 2026-10-25

Added a distinct splash for a VRX client connection with an assigned IP address while waiting for video. Older VRX units may require an active VTX before RTSP starts; the label describes the expected next step, not a verified diagnosis of transmitter state. All 31 local automated tests pass, including network-to-splash selection. Installer and deployment include the new asset. Not yet deployed or validated on hardware because the Pi is not reachable at its saved home address. Playback preset is unchanged.

## Setup and VTX splashes deployed ? 2026-10-25

All 32 local tests pass. Deployed WAITING FOR VTX and red SETUP MODE assets and state selection. SSH verified the active service, setup splash process, and asset SHA-256 hashes. Setup mode remains selected on saved home Wi-Fi. Current-boot viewer and NetworkManager logs before setup selection contain no ASCENT discovery or join attempt; scans only showed other networks. VRX power timing is being checked with the user. VTX splash transition still needs hardware validation. Deployment now transfers images by SCP to avoid oversized SSH commands.

## Scan completion and playback audit ? 2026-10-25

Automatic trial: ASCENT-example discovered at 08:55:04, client connection at 08:55:07, VTX splash at 08:55:08. Repeated player failures report streaming stopped, reason not-negotiated (-4), from RTSP UDP source. This confirms joining succeeded and media negotiation failed; codec/resolution cause remains unverified. Independent timed recovery restored setup mode at 08:57:27.

Scan repair now checks NetworkManager Wireless.LastScan over D-Bus before accepting AP results; unchanged timestamps time out as a scan error, never as an empty successful scan. Station reset suspends device autoconnect to avoid the observed home-profile activation race. Setup/recovery restore autoconnect. All 33 local tests pass. Deployed with backups under /var/backups/ascent-scan-fix-*, verified LastScan read and completed live station scan in 0.991 seconds showing ASCENT-example. Service active. AP-to-station cycling with this repair remains unvalidated; do not describe the original scan issue as conclusively resolved. Device deliberately held in setup mode pending media settings and diagnosis.

## Release 1.0.5 ? older VRX compatibility and visible version

Older VRX spectator60 settings supplied by the user: hardware H.264 CBR, 1920x1080, requested 60 fps, 4,194,304 bps, GOP 60, RTP clock 90 kHz with 1500 ticks per callback, /live.ch01; TCP intended. These differ from the previously approved MAX playback context and cannot be inferred from SSID alone.

With an active VTX link, sequential eight-second captures succeeded over UDP and TCP (3,142 and 3,169 RTP packets). Both hardware decode tests rejected colorimetry 1:4:16:4. Offline A/B against the same capture: original zero decoded frames and negotiation error; BT.709 relabel 480 decoded frames; conditional relabel also 480 frames. This demonstrates the negotiation fix, not a calibrated color-space measurement.

Live UDP after the color fix displayed video but the user reported five-second delay and severe motion smearing. TCP with the same decode/display configuration was visually approved as amazing; counters show about 60 decoded fps near the end of the TCP run, versus roughly 30 during UDP. The exact mechanism of UDP degradation was not established by these tests.

Runtime compatibility now selects TCP for this observed stream colorimetry, without guessing model from AP names. Other metadata retains configured transport. Explicit TCP remains TCP. The MAX baseline is unchanged and has not been retested with this release. All 35 local automated tests pass. Final automatic-selection validation is recorded below when completed.

All splashes now overlay the installed version from VERSION, with matching release packaging and API field. Installer includes the Pango textoverlay plugin package (gstreamer1.0-x); this small package was installed and the plugin inspected on the Pi.

Final automatic-selection trial (09:32:41?09:33:27): completed scan found ASCENT-example; join succeeded; first player recognized the observed color tuple and requested transport restart code 3; second player source metadata reported TCP and decoding/display frames arrived. Thus automatic metadata selection works. However the user reported poor picture quality again despite TCP. The earlier TCP success must not be generalized to a reliable fix for motion smearing. Quality remains unresolved; compare phone/source behavior and measure jitter/drop counters before further tuning.

## Authoritative current status ? 1.0.5-rc1

**Older VRX motion playback FAILED. Do not describe the buffered receiver or TCP as a proven fix.** The user reported frozen/very slow motion and over five seconds of delay. The automatic buffered-UDP run recorded 444 RTP sequence-gap events, discarded over 1,100 dependent NALs waiting for keyframes, and repeatedly entered waiting/retry states. Keyframe gating traded corruption for freezing. Prior zero-error static captures demonstrated decodability only, not acceptable throughput or camera-to-display latency. The experimental receiver is now disabled for automatic selection.

The VTX was disconnected by the user. Further testing used saved files and a loopback-only RTSP/RTP replay server, without requiring user participation. Replaying the clean old-VRX H.264 at 60 fps on the Pi produced 1,256 decoded frames over roughly 21 active seconds, zero RTP gaps in the buffered receiver, and an intact display snapshot; adding frequent nmcli status polling also produced 1,259 decoded frames and zero RTP gaps. Normal GStreamer reception decoded 1,196 frames. These tests show that the same codec can be decoded at approximately source rate locally. They do not prove adequate real-VRX Wi-Fi reception or end-to-end latency. Display output remains roughly 13?15 fps in these trials despite a 30 fps limit; that limit is a ceiling.

Capture-only TCP received 600 pictures in 10 seconds with continuous frame numbering; UDP received 599, with one missing picture. Tests with additional decoder/display/CPU work sometimes degraded reception, but the precise causal mechanism remains unresolved. Larger TCP buffers, realtime receive priority, TCP_QUICKACK, compressed queueing, alternate RTSP receivers, and timestamp changes did not produce a reliable motion fix. RAM-only capture plus a larger UDP buffer gave a clean short test, then failed the subsequent real motion test.

An older receiver was observed accepting Wi-Fi association and DHCP with RTSP available. This bounded observation does not establish sustained video playback quality.

All 38 current local automated tests pass. This does not override the failed hardware motion test. Both added splash assets and the version/build overlay are present; installer/deployment include their dependencies. Keep the Pi in setup mode on saved Wi-Fi while diagnosing. Do not publish this candidate as a completed old-VRX playback repair.

Final candidate verification: version 1.0.5-rc1, source fingerprint 7d3dae4e, matching on Windows and the Pi. All 38 tests pass on Windows/Python 3.14 and Pi/Python 3.11. All four splash states rendered through GStreamer with the version/build overlay. Installer shell syntax passed after normalizing its LF line endings. Release ZIP paths use forward slashes; installer payload integrity is checked. Failed receiver automatic selection is disabled. Pi configuration API confirms setup mode true and playback setup mode on saved home Wi-Fi. No further live VTX tests were run after the user disconnected it.

Splash assets were generated with the built-in imagegen tool: assets/waiting-vtx.png used the existing waiting image as an edit target, preserving artwork and replacing only WAITING FOR VRX with WAITING FOR VTX; assets/setup.png preserved the same artwork and replaced the banner with SETUP MODE in vivid red. Version labels are rendered by GStreamer rather than baked into the artwork.

## 1.0.5-rc2: rendering and diagnostic qualification

The previous live moving-camera failure remains unresolved. The following controls qualify a rendering improvement, not the older VRX's live RTSP producer or the complete camera-to-screen path.

An independently generated, numbered 1920x1080/60 High-profile CABAC clip was replayed over physical home Wi-Fi at approximately 4.2 Mbps. Each picture has one slice; GOP60, no B frames. Source RTP timestamps and repeated clock calibrations measured frame age before LCD scanout.

| Controlled phase | Duration | Median display FPS | Process CPU cores | Median / p95 frame age |
|---|---:|---:|---:|---:|
| Approved standard graph | 5 min | 12.96 | 1.18 | 183 / 266 ms |
| Software stage queues | 20 min | 29.18 | 2.63 | 164 / 250 ms |
| ISP plus cached conversion | 20 min | 29.63 | 1.84 | 164 / 249 ms |
| DMA ISP plus cached conversion | 20 min | 29.79 | 1.80 | 155 / 233 ms |

Clock uncertainty at the 95th percentile was approximately 44–65 ms. These are replay-to-pre-display measurements, excluding camera exposure, VRX encoding and LCD scanout. All phases completed, without kernel UDP receive errors or IP discards; no thermal throttling was observed. Occasional jitter-buffer latency-policy drops were measured separately from network loss.

A further three-minute A/B of the actual staged player reduced CPU from 1.82 to 1.19 cores by limiting decoded raw frames before expensive conversion. Recent display samples remained near30fps. This FPS limiter never discards compressed H.264 reference pictures; jitter-buffer losses remain separately reported. The failed compressed-frame keyframe gate remains disabled.

All32 sampled output frames across the long comparison and early-drop A/B matched software-decoded references, SSIM0.99927 or higher. The first hardware reference was invalid because downstream ISP negotiation induced an extra software colour conversion. Fixing the reference to preserve decoded YUV bytes resolved the discrepancy; production pixel processing was not changed to improve the score. The initial mismatched-reference results remain in the private audit evidence.

Real controller testing exposed a DMA allocation stall when KMS was activated late. The selected hardware child now requests splash release before constructing the DMA/KMS pipeline. Reconnects first probe the stream so WAITING FOR VTX remains available while RTSP is absent. The actual controller subsequently selected hardware from legacy metadata, survived an injected renderer setup failure by falling back once, recovered hardware on reconnect, and returned to the standard graph with normal metadata. A further replay of the original clean old-VRX recording exercised its much larger IDRs and original SPS/POC layout: approximately60fps decoded and27–30fps delivered to the display over several minutes.

Selection requires the observed exact colour tuple and1920x1080 geometry; it is not a claim to identify a VRX model. Other metadata/dimensions use the unchanged MAX graph. UDP/200ms, H.264 hardware decoding, RGB16, rotation, FPS ceiling and watchdog settings remain unchanged. No VRX encoder or firmware settings were changed.

Diagnostics now distinguish source-ID changes, duplicates, reordering, sequence gaps and reported jitter-policy drops, and expose decode/display rates, internal PTS backlog, last-frame time and sample age. These do not measure camera-to-screen latency. The H.264 inspector reads the SPS frame-number width and reports reference discontinuities with explicit limits for field pictures/MMCO5/redundant pictures. A real TCP regression test covers partial reads, fragmented RTP, wraparound, RTCP and keepalive/media coalescing without omitted or repeated NALs.

Fresh installation now includes capture_rtsp.py as well as legacy_receiver.py, making its core fingerprint consistent with deployment. The inspector and its tests are included in the release. All53 tests passed on Windows and against the installed modules on the Pi. Installed version1.0.5-rc2, fingerprint95b6353b. Final installed-code soak details are recorded in the private continuation report.

Actual ASCENT-example ICMP controls, with VTX off, found a processing/radio interaction. With1400-byte pings at the higher requested rate, average RTT was9.36ms idle,53.28ms decode-only,73.92ms with the standard software renderer,19.26ms with cached hardware rendering, and10.65ms idle again. Short-phase loss was0-0.6percent. A separate DMA/early-drop run received all2000 ping replies during rendering; its higher-rate average was11.72ms, compared with surrounding idle values9.37 and8.01ms. Power saving remained off. DESCRIBE timed out in both AP audits. These short ICMP tests support a workload effect; they do not qualify live RTSP integrity or prove the full cause of the previous smearing.
