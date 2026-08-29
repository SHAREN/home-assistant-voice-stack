# Roadmap

## P610 speaker volume in Home Assistant

Current status: planned.

The active Pipecat/P610 path plays assistant PCM through pacat/PulseAudio, but it does not currently expose the physical playback sink as a Home Assistant media_player entity. A legacy Plantronics P610 media_player may still exist in Home Assistant from the old Assist Satellite setup, but it is not part of the current Pipecat local-audio path and can be unavailable when the legacy satellite is disabled.

Planned implementation:

- expose the active P610 PulseAudio sink through the current Pipecat Assist integration;
- create a media_player entity with device_class speaker;
- support volume_level readback;
- support media_player.volume_set;
- support volume_up / volume_down;
- support volume_mute;
- keep microphone gain as a separate control from speaker playback volume;
- preserve full-duplex/barge-in behavior;
- expose the selected sink and current volume in Pipecat status/debug data;
- add regression tests so changing volume cannot restart or interrupt the realtime pipeline.

The control should target the PulseAudio sink used by pacat, not the retired Assist Satellite media player.
