# Roadmap

## P610 speaker volume in Home Assistant

Current status: planned.

The active Pipecat/P610 path plays assistant PCM through pacat/PulseAudio, but it does not currently expose the physical playback sink as a Home Assistant media_player entity. A legacy Plantronics P610 media_player may still exist in Home Assistant from the old Assist Satellite setup, but it is not part of the current Pipecat local-audio path and can be unavailable when the legacy satellite is disabled.

Planned implementation:

- expose the active P610 PulseAudio sink through the current Pipecat Assist integration;
- create a volume-only media_player entity with device_class speaker;
- support volume_level readback;
- support media_player.volume_set;
- support volume_up / volume_down;
- support volume_mute;
- do not expose play, pause, stop, play_media, TTS, source selection, browsing, or any other media-start capability;
- changing volume or mute must never start playback or inject audio into the P610 output path;
- the entity exists only as a Home Assistant control surface for the sink already used by Pipecat/pacat;
- keep microphone gain as a separate control from speaker playback volume;
- preserve full-duplex/barge-in behavior;
- expose the selected sink and current volume in Pipecat status/debug data;
- add regression tests so changing volume cannot restart or interrupt the realtime pipeline and cannot start audio playback.

The control should target the PulseAudio sink used by pacat, not the retired Assist Satellite media player.
