param(
    [Parameter(Mandatory = $true)]
    [string]$PhraseWav,
    [Parameter(Mandatory = $true)]
    [string]$ExpectedOutputName,
    [int[]]$Levels = @(80, 60, 40),
    [ValidateRange(1, 10)]
    [int]$AttemptsPerLevel = 2,
    [string]$EntityId = 'assist_satellite.plantronics_p610_assist_satellite',
    [string]$SilentMediaId = 'media-source://media_source/local/assist_stt_start_silence.wav',
    [string]$ResultsPath = (Join-Path $env:TEMP 'p610_stt_calibration.jsonl'),
    [bool]$StopAfterFailure = $true,
    [switch]$PreflightOnly,
    [switch]$AllowExcludedOutput
)

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$ExcludedOutputPattern = '(?i)Beyond|\bTV\b|Steam|Digital|HDMI|Quantum350|Headphones?|Headsets?|Наушник'

Add-Type -AssemblyName System.Net.Http
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public enum EDataFlow { eRender, eCapture, eAll }
public enum ERole { eConsole, eMultimedia, eCommunications }
[ComImport, Guid("D666063F-1587-4E43-81F1-B948E807363F"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IMMDevice { int Activate(ref Guid iid, int clsctx, IntPtr parameters, out IntPtr result); int OpenPropertyStore(int access, out IntPtr properties); int GetId([MarshalAs(UnmanagedType.LPWStr)] out string id); int GetState(out int state); }
[ComImport, Guid("A95664D2-9614-4F35-A746-DE8DB63617E6"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IMMDeviceEnumerator { int EnumAudioEndpoints(EDataFlow flow, int mask, out IntPtr devices); int GetDefaultAudioEndpoint(EDataFlow flow, ERole role, out IMMDevice device); int GetDevice([MarshalAs(UnmanagedType.LPWStr)] string id, out IMMDevice device); int RegisterEndpointNotificationCallback(IntPtr callback); int UnregisterEndpointNotificationCallback(IntPtr callback); }
[ComImport, Guid("5CDF2C82-841E-4546-9722-0CF74078229A"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IAudioEndpointVolume {
    int RegisterControlChangeNotify(IntPtr notify);
    int UnregisterControlChangeNotify(IntPtr notify);
    int GetChannelCount(out uint count);
    int SetMasterVolumeLevel(float levelDb, ref Guid eventContext);
    int SetMasterVolumeLevelScalar(float level, ref Guid eventContext);
    int GetMasterVolumeLevel(out float levelDb);
    int GetMasterVolumeLevelScalar(out float level);
    int SetChannelVolumeLevel(uint channel, float levelDb, ref Guid eventContext);
    int SetChannelVolumeLevelScalar(uint channel, float level, ref Guid eventContext);
    int GetChannelVolumeLevel(uint channel, out float levelDb);
    int GetChannelVolumeLevelScalar(uint channel, out float level);
    int SetMute([MarshalAs(UnmanagedType.Bool)] bool muted, ref Guid eventContext);
    int GetMute([MarshalAs(UnmanagedType.Bool)] out bool muted);
}
[ComImport, Guid("BCDE0395-E52F-467C-8E3D-C4579291692E")] class MMDeviceEnumeratorComObject {}
public sealed class RenderEndpointState {
    public string Id;
    public float MasterVolumeScalar;
    public bool Muted;
}
public static class DefaultRenderDevice {
    public static RenderEndpointState GetState() {
        var e=(IMMDeviceEnumerator)new MMDeviceEnumeratorComObject();
        IMMDevice d;
        Marshal.ThrowExceptionForHR(e.GetDefaultAudioEndpoint(EDataFlow.eRender, ERole.eMultimedia, out d));
        string id;
        Marshal.ThrowExceptionForHR(d.GetId(out id));
        var iid=typeof(IAudioEndpointVolume).GUID;
        IntPtr pointer;
        Marshal.ThrowExceptionForHR(d.Activate(ref iid, 23, IntPtr.Zero, out pointer));
        var volume=(IAudioEndpointVolume)Marshal.GetObjectForIUnknown(pointer);
        float scalar;
        bool muted;
        Marshal.ThrowExceptionForHR(volume.GetMasterVolumeLevelScalar(out scalar));
        Marshal.ThrowExceptionForHR(volume.GetMute(out muted));
        Marshal.Release(pointer);
        return new RenderEndpointState { Id=id, MasterVolumeScalar=scalar, Muted=muted };
    }
}
'@

function Get-YekaterinburgNow {
    $zone = [System.TimeZoneInfo]::FindSystemTimeZoneById('Ekaterinburg Standard Time')
    return [System.TimeZoneInfo]::ConvertTimeFromUtc([DateTime]::UtcNow, $zone)
}

function Assert-BeforeQuietDeadline([double]$RequiredSeconds) {
    $now = Get-YekaterinburgNow
    $deadline = $now.Date.AddHours(20)
    if ($now.AddSeconds($RequiredSeconds) -ge $deadline) {
        throw "Physical playback forbidden: local time $($now.ToString('HH:mm:ss')), required safe window ${RequiredSeconds}s"
    }
}

function Get-DefaultOutput {
    $state = [DefaultRenderDevice]::GetState()
    $id = $state.Id
    $endpointKey = [regex]::Match($id, '\{[0-9a-fA-F-]+\}$').Value
    $path = "Registry::HKEY_LOCAL_MACHINE\SOFTWARE\Microsoft\Windows\CurrentVersion\MMDevices\Audio\Render\$endpointKey\Properties"
    $properties = Get-ItemProperty -LiteralPath $path
    return [PSCustomObject]@{
        Id = $id
        Name = $properties.'{a45c254e-df1c-4efd-8020-67d146a850e0},2'
        Driver = $properties.'{b3f8fa53-0004-438e-9003-51a46e139bfc},6'
        MasterVolumePercent = [Math]::Round([double]$state.MasterVolumeScalar * 100, 1)
        Muted = [bool]$state.Muted
    }
}

function Assert-AllowedOutput([object]$Candidate, [string]$ExpectedName, [bool]$PermitExcluded) {
    if ($Candidate.Name -notlike "*$ExpectedName*" -and $Candidate.Driver -notlike "*$ExpectedName*") {
        throw "Default output '$($Candidate.Name)' / '$($Candidate.Driver)' does not match '$ExpectedName'"
    }
    if (-not $PermitExcluded -and "$($Candidate.Name) $($Candidate.Driver)" -match $ExcludedOutputPattern) {
        throw "Excluded default output: $($Candidate.Name) / $($Candidate.Driver)"
    }
    if ($Candidate.Muted) {
        throw "Default output is muted: $($Candidate.Name) / $($Candidate.Driver)"
    }
}

function Invoke-HaGet([string]$Uri, [hashtable]$Headers) {
    return Invoke-RestMethod -Method Get -Uri $Uri -Headers $Headers -TimeoutSec 10
}

function Get-P610ProfileSnapshot([string]$HaUrl, [hashtable]$Headers) {
    $states = @{}
    foreach ($entityId in @(
        'select.plantronics_p610_wake_word',
        'number.plantronics_p610_wake_word_1_sensitivity',
        'number.plantronics_p610_mic_volume',
        'number.plantronics_p610_mic_auto_gain',
        'select.plantronics_p610_mic_noise_suppression',
        'switch.plantronics_p610_mute'
    )) {
        $states[$entityId] = Invoke-HaGet "$HaUrl/api/states/$entityId" $Headers
    }
    return [PSCustomObject]@{
        WakeWord = $states['select.plantronics_p610_wake_word'].state
        WakeSensitivity = [double]$states['number.plantronics_p610_wake_word_1_sensitivity'].state
        MicVolume = [double]$states['number.plantronics_p610_mic_volume'].state
        MicAutoGain = [double]$states['number.plantronics_p610_mic_auto_gain'].state
        MicNoiseSuppression = $states['select.plantronics_p610_mic_noise_suppression'].state
        Muted = $states['switch.plantronics_p610_mute'].state -eq 'on'
    }
}

function Get-LatestSttMetrics {
    $raw = & ssh ha 'test -f /config/voice_debug/latest_stt_metrics.json && cat /config/voice_debug/latest_stt_metrics.json'
    if ($LASTEXITCODE -ne 0 -or -not $raw) { return $null }
    try {
        return (($raw -join "`n") | ConvertFrom-Json)
    }
    catch {
        return $null
    }
}

function Get-LatestLvaCaptureMetrics {
    $raw = & ssh ha 'test -f /share/voice_debug/latest_lva_stt_pcm_metrics.json && cat /share/voice_debug/latest_lva_stt_pcm_metrics.json'
    if ($LASTEXITCODE -ne 0 -or -not $raw) { return $null }
    try {
        return (($raw -join "`n") | ConvertFrom-Json)
    }
    catch {
        return $null
    }
}

function Wait-SatelliteIdle([string]$StateUri, [hashtable]$Headers, [int]$TimeoutSeconds = 15) {
    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    do {
        $state = Invoke-HaGet $StateUri $Headers
        if ($state.state -eq 'idle') { return $true }
        Start-Sleep -Milliseconds 200
    } while ([DateTime]::UtcNow -lt $deadline)
    return $false
}

function Add-RecordError([System.Collections.Specialized.OrderedDictionary]$Record, [string]$Message) {
    if (-not $Message) { return }
    if ($Record.error) {
        $Record.error = "$($Record.error); $Message"
    }
    else {
        $Record.error = $Message
    }
}

function Test-FiniteNumber([object]$Value) {
    if ($null -eq $Value) { return $false }
    if ($Value -is [bool] -or $Value -is [string]) { return $false }
    try {
        $number = [double]$Value
        return -not [double]::IsNaN($number) -and -not [double]::IsInfinity($number)
    }
    catch {
        return $false
    }
}

function Test-GeminiMetricsPayload([object]$Metrics) {
    if ($null -eq $Metrics -or
        -not (Test-FiniteNumber $Metrics.schema_version) -or
        [int]$Metrics.schema_version -ne 1) { return $false }
    if (-not $Metrics.turn_id -or $Metrics.outcome -notin @('stt_success', 'stt_failed')) { return $false }
    if ($Metrics.input_audio_sent -isnot [bool] -or $null -eq $Metrics.pcm) { return $false }
    foreach ($field in @('pcm_bytes', 'duration_seconds', 'rms_percent', 'peak_percent')) {
        if (-not (Test-FiniteNumber $Metrics.pcm.$field)) { return $false }
        if ([double]$Metrics.pcm.$field -lt 0) { return $false }
    }
    if ([double]$Metrics.pcm.rms_percent -gt 100 -or [double]$Metrics.pcm.peak_percent -gt 100) {
        return $false
    }
    return $true
}

function Test-PcmAggregatePayload([object]$Stage) {
    if ($null -eq $Stage) { return $false }
    foreach ($field in @('blocks', 'pcm_bytes', 'duration_seconds', 'rms_percent', 'peak_percent')) {
        if (-not (Test-FiniteNumber $Stage.$field)) { return $false }
        if ([double]$Stage.$field -lt 0) { return $false }
    }
    if ([double]$Stage.blocks % 1 -ne 0 -or [double]$Stage.pcm_bytes % 1 -ne 0) {
        return $false
    }
    if ([double]$Stage.rms_percent -gt 100 -or [double]$Stage.peak_percent -gt 100) {
        return $false
    }
    return $true
}

function Test-LvaMetricsPayload([object]$Metrics) {
    if ($null -eq $Metrics -or
        -not (Test-FiniteNumber $Metrics.schema_version) -or
        [int]$Metrics.schema_version -ne 1) { return $false }
    if (-not $Metrics.process_instance_id -or -not $Metrics.window_id) { return $false }
    foreach ($field in @('capture_sequence', 'satellite_generation', 'sample_rate', 'block_size', 'boundary_tolerance_ms')) {
        if (-not (Test-FiniteNumber $Metrics.$field)) { return $false }
        if ([double]$Metrics.$field -lt 0) { return $false }
    }
    if ([double]$Metrics.capture_sequence % 1 -ne 0 -or [double]$Metrics.capture_sequence -lt 1) {
        return $false
    }
    if ([double]$Metrics.satellite_generation % 1 -ne 0 -or [double]$Metrics.satellite_generation -lt 1) {
        return $false
    }
    if ([int]$Metrics.sample_rate -ne 16000 -or [double]$Metrics.block_size -le 0) {
        return $false
    }
    $expectedBoundaryMs = [double]$Metrics.block_size / [double]$Metrics.sample_rate * 1000
    if ([Math]::Abs([double]$Metrics.boundary_tolerance_ms - $expectedBoundaryMs) -gt 0.001) {
        return $false
    }
    if ($Metrics.reason -ne 'streaming_ended' -or $Metrics.settings_changed -isnot [bool]) {
        return $false
    }
    if (-not $Metrics.started_at -or -not $Metrics.captured_at -or $null -eq $Metrics.settings) {
        return $false
    }
    if (-not (Test-FiniteNumber $Metrics.settings.mic_volume) -or
        -not (Test-FiniteNumber $Metrics.settings.mic_auto_gain) -or
        -not (Test-FiniteNumber $Metrics.settings.mic_noise_suppression) -or
        $Metrics.settings.muted -isnot [bool]) {
        return $false
    }
    if (-not (Test-PcmAggregatePayload $Metrics.pre_webrtc) -or
        -not (Test-PcmAggregatePayload $Metrics.post_webrtc)) {
        return $false
    }
    return $true
}

function Get-HaAudioSnapshot {
    $monitorInfo = & ssh ha 'ha apps info local_p610_mic_monitor'
    $monitorInfoText = $monitorInfo -join "`n"
    if ($LASTEXITCODE -ne 0 -or
        $monitorInfoText -notmatch '(?m)^state: stopped$' -or
        $monitorInfoText -notmatch '(?m)^boot: manual$') {
        throw 'P610 monitor must be stopped with boot manual'
    }
    $audioInfo = & ssh ha 'ha audio info --raw-json'
    if ($LASTEXITCODE -ne 0) { throw 'Could not read HA Audio state' }
    $audioPayload = (($audioInfo -join "`n") | ConvertFrom-Json)
    $voiceStreams = @($audioPayload.data.audio.application | Where-Object { $_.name -eq 'linux_voice_assistant' })
    if ($voiceStreams.Count -ne 1) { throw "Expected one linux_voice_assistant stream, found $($voiceStreams.Count)" }
    $defaultInput = @($audioPayload.data.audio.input | Where-Object { $_.default }) | Select-Object -First 1
    if ($null -eq $defaultInput) { throw 'Default HA Audio input is missing' }
    return [PSCustomObject]@{
        MonitorState = 'stopped'
        VoiceStreamCount = $voiceStreams.Count
        DefaultInput = $defaultInput
    }
}

function Get-SatelliteAppSnapshot {
    $satelliteInfo = & ssh ha 'ha apps info local_assist_satellite_session_end'
    if ($LASTEXITCODE -ne 0) { throw 'Could not read Assist Satellite App state' }
    $text = $satelliteInfo -join "`n"
    foreach ($required in @(
        '(?m)^state: started$',
        '(?m)^boot: auto$',
        '(?m)^watchdog: false$',
        '(?m)^version: 1\.1\.15-p610\.4$',
        '(?m)^  session_end_sound: /usr/src/sounds/session_end\.wav$',
        '(?m)^  stt_pcm_metrics_path: /share/voice_debug/latest_lva_stt_pcm_metrics\.json$'
    )) {
        if ($text -notmatch $required) {
            throw "Assist Satellite invariant missing: $required"
        }
    }
    return [PSCustomObject]@{
        State = 'started'
        Boot = 'auto'
        Watchdog = $false
        Version = '1.1.15-p610.4'
        SessionEndSound = '/usr/src/sounds/session_end.wav'
        MetricsPath = '/share/voice_debug/latest_lva_stt_pcm_metrics.json'
    }
}

if (-not (Test-Path -LiteralPath $PhraseWav -PathType Leaf)) {
    throw "Phrase WAV not found: $PhraseWav"
}
if (-not (Get-Command ffplay.exe -ErrorAction SilentlyContinue)) { throw 'ffplay.exe not found' }
if (-not (Get-Command ffprobe.exe -ErrorAction SilentlyContinue)) { throw 'ffprobe.exe not found' }

$output = Get-DefaultOutput
Assert-AllowedOutput $output $ExpectedOutputName ([bool]$AllowExcludedOutput)
if ($AllowExcludedOutput -and -not $PreflightOnly) {
    throw '-AllowExcludedOutput is permitted only with -PreflightOnly'
}

$haAudioSnapshot = Get-HaAudioSnapshot
$defaultInput = $haAudioSnapshot.DefaultInput
$satelliteSnapshot = Get-SatelliteAppSnapshot
$configuredLvaMetricsPath = $satelliteSnapshot.MetricsPath
$durationText = & ffprobe.exe -v error -show_entries format=duration -of 'default=noprint_wrappers=1:nokey=1' -- $PhraseWav
if ($LASTEXITCODE -ne 0) { throw 'ffprobe could not read phrase duration' }
$phraseDuration = [double]::Parse(($durationText | Select-Object -First 1), [Globalization.CultureInfo]::InvariantCulture)
$phraseSha256 = (Get-FileHash -LiteralPath $PhraseWav -Algorithm SHA256).Hash.ToLowerInvariant()

$haUrl = if ($env:HA_URL) { $env:HA_URL.TrimEnd('/') } else { 'http://homeassistant.local:8123' }
$token = $env:HOMEASSISTANT_TOKEN
if (-not $token) { throw 'HOMEASSISTANT_TOKEN is not set' }
$headers = @{ Authorization = "Bearer $token"; 'Content-Type' = 'application/json' }

$repoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..')).TrimEnd([IO.Path]::DirectorySeparatorChar, [IO.Path]::AltDirectorySeparatorChar)
$resultsFullPath = [IO.Path]::GetFullPath($ResultsPath)
if ($resultsFullPath.Equals($repoRoot, [StringComparison]::OrdinalIgnoreCase) -or
    $resultsFullPath.StartsWith($repoRoot + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'ResultsPath must be outside the Git worktree because it may contain recognized text'
}
$resultsDirectory = Split-Path -Parent $resultsFullPath
if (-not (Test-Path -LiteralPath $resultsDirectory -PathType Container)) {
    throw "ResultsPath parent directory does not exist: $resultsDirectory"
}

$stateUri = "$haUrl/api/states/$EntityId"
$initialState = Invoke-HaGet $stateUri $headers
if ($initialState.state -ne 'idle') { throw "Satellite must be idle, got $($initialState.state)" }
$initialProfile = Get-P610ProfileSnapshot $haUrl $headers

$preflight = [ordered]@{
    local_time = (Get-YekaterinburgNow).ToString('o')
    output_name = $output.Name
    output_driver = $output.Driver
    output_endpoint_id = $output.Id
    output_master_volume_percent = $output.MasterVolumePercent
    output_muted = $output.Muted
    phrase_duration_seconds = [Math]::Round($phraseDuration, 3)
    phrase_sha256 = $phraseSha256
    levels = $Levels
    attempts_per_level = $AttemptsPerLevel
    monitor_state = $haAudioSnapshot.MonitorState
    linux_voice_assistant_streams = $haAudioSnapshot.VoiceStreamCount
    system_input_name = $defaultInput.name
    system_input_volume_percent = [Math]::Round([double]$defaultInput.volume * 100, 1)
    system_input_muted = [bool]$defaultInput.mute
    lva_capture_metrics_path = $configuredLvaMetricsPath
    satellite_version = $satelliteSnapshot.Version
    satellite_boot = $satelliteSnapshot.Boot
    satellite_watchdog = $satelliteSnapshot.Watchdog
    session_end_sound = $satelliteSnapshot.SessionEndSound
    wake_word = $initialProfile.WakeWord
    wake_sensitivity = $initialProfile.WakeSensitivity
    mic_volume = $initialProfile.MicVolume
    mic_auto_gain = $initialProfile.MicAutoGain
    mic_noise_suppression = $initialProfile.MicNoiseSuppression
    mic_muted = $initialProfile.Muted
}
if ($PreflightOnly) {
    $preflight | ConvertTo-Json -Depth 4
    exit 0
}

foreach ($level in $Levels) {
    for ($attemptIndex = 1; $attemptIndex -le $AttemptsPerLevel; $attemptIndex++) {
    $record = [ordered]@{
        status = 'failed'
        error = $null
        local_time = (Get-YekaterinburgNow).ToString('o')
        level_percent = $level
        attempt_index = $attemptIndex
        attempts_per_level = $AttemptsPerLevel
        phrase_duration_seconds = [Math]::Round($phraseDuration, 3)
        phrase_sha256 = $phraseSha256
        output_name = $output.Name
        output_driver = $output.Driver
        output_endpoint_id = $output.Id
        output_master_volume_percent = $output.MasterVolumePercent
        output_muted = $output.Muted
        effective_output_percent = $null
        system_input_name = $defaultInput.name
        system_input_volume_percent = [Math]::Round([double]$defaultInput.volume * 100, 1)
        system_input_muted = [bool]$defaultInput.mute
        wake_word = $initialProfile.WakeWord
        wake_sensitivity = $initialProfile.WakeSensitivity
        mic_volume = $initialProfile.MicVolume
        mic_auto_gain = $initialProfile.MicAutoGain
        mic_noise_suppression = $initialProfile.MicNoiseSuppression
        mic_muted = $initialProfile.Muted
        satellite_version = $satelliteSnapshot.Version
        satellite_boot = $satelliteSnapshot.Boot
        satellite_watchdog = $satelliteSnapshot.Watchdog
        session_end_sound = $satelliteSnapshot.SessionEndSound
        request_started_utc = $null
        listening_observed_utc = $null
        request_completed_utc = $null
        play_started_local = $null
        http_status = $null
        stt_response = $null
        previous_metrics_turn_id = $null
        previous_lva_metrics_key = $null
        previous_lva_process_instance_id = $null
        previous_lva_capture_sequence = $null
        metrics_status = 'not_requested'
        stt_metrics = $null
        lva_metrics_status = 'not_requested'
        lva_capture_metrics = $null
        satellite_idle = $null
        post_attempt_audio_healthy = $null
        post_attempt_lva_streams = $null
        post_attempt_input_name = $null
        post_attempt_input_volume_percent = $null
        post_attempt_input_muted = $null
        continuation_safe = $false
    }
    $client = $null
    $content = $null
    $response = $null
    $task = $null
    $requestStarted = $null
    $listeningObserved = $null
    $requestCompleted = $null
    $acceptedMetricsCapturedAt = $null
    $acceptedLvaStartedAt = $null
    $acceptedLvaCapturedAt = $null
    $acceptedLvaBoundaryMs = $null
    $requestCreated = $false

    try {
        if ($level -lt 0 -or $level -gt 100) { throw "Invalid level: $level" }
        $attemptOutput = Get-DefaultOutput
        Assert-AllowedOutput $attemptOutput $ExpectedOutputName $false
        if ($attemptOutput.Id -ne $output.Id) {
            throw "Default output endpoint changed from '$($output.Id)' to '$($attemptOutput.Id)'"
        }
        if ([Math]::Abs([double]$attemptOutput.MasterVolumePercent - [double]$output.MasterVolumePercent) -gt 0.1) {
            throw "Default output master volume changed from $($output.MasterVolumePercent)% to $($attemptOutput.MasterVolumePercent)%"
        }
        $record.output_name = $attemptOutput.Name
        $record.output_driver = $attemptOutput.Driver
        $record.output_endpoint_id = $attemptOutput.Id
        $record.output_master_volume_percent = $attemptOutput.MasterVolumePercent
        $record.output_muted = $attemptOutput.Muted
        $record.effective_output_percent = [Math]::Round(
            [double]$attemptOutput.MasterVolumePercent * $level / 100,
            2
        )

        $attemptHaAudio = Get-HaAudioSnapshot
        $attemptInput = $attemptHaAudio.DefaultInput
        if ($attemptInput.name -ne $defaultInput.name) {
            throw "Default HA input changed from '$($defaultInput.name)' to '$($attemptInput.name)'"
        }
        if ([Math]::Abs([double]$attemptInput.volume - [double]$defaultInput.volume) -gt 0.0001 -or
            [bool]$attemptInput.mute -ne [bool]$defaultInput.mute) {
            throw 'Default HA input volume or mute state changed during calibration'
        }
        $record.system_input_name = $attemptInput.name
        $record.system_input_volume_percent = [Math]::Round([double]$attemptInput.volume * 100, 1)
        $record.system_input_muted = [bool]$attemptInput.mute

        $attemptSatellite = Get-SatelliteAppSnapshot
        if ($attemptSatellite.Version -ne $satelliteSnapshot.Version -or
            $attemptSatellite.SessionEndSound -ne $satelliteSnapshot.SessionEndSound -or
            $attemptSatellite.MetricsPath -ne $satelliteSnapshot.MetricsPath) {
            throw 'Assist Satellite version/cue/metrics invariants changed during calibration'
        }

        $attemptProfile = Get-P610ProfileSnapshot $haUrl $headers
        if (
            $attemptProfile.WakeWord -ne $initialProfile.WakeWord -or
            [Math]::Abs($attemptProfile.WakeSensitivity - $initialProfile.WakeSensitivity) -gt 0.0001 -or
            [Math]::Abs($attemptProfile.MicVolume - $initialProfile.MicVolume) -gt 0.0001 -or
            [Math]::Abs($attemptProfile.MicAutoGain - $initialProfile.MicAutoGain) -gt 0.0001 -or
            $attemptProfile.MicNoiseSuppression -ne $initialProfile.MicNoiseSuppression -or
            $attemptProfile.Muted -ne $initialProfile.Muted
        ) {
            throw 'P610 wake/microphone profile changed during calibration'
        }
        $record.wake_word = $attemptProfile.WakeWord
        $record.wake_sensitivity = $attemptProfile.WakeSensitivity
        $record.mic_volume = $attemptProfile.MicVolume
        $record.mic_auto_gain = $attemptProfile.MicAutoGain
        $record.mic_noise_suppression = $attemptProfile.MicNoiseSuppression
        $record.mic_muted = $attemptProfile.Muted

        # Reserve the complete lifecycle: listening gate, phrase, STT response,
        # local final cue and return to idle. Do not start a request which could
        # leave any satellite audio running across the 20:00 quiet boundary.
        Assert-BeforeQuietDeadline ($phraseDuration + 75)

        $previousMetrics = Get-LatestSttMetrics
        if ($null -ne $previousMetrics) {
            $record.previous_metrics_turn_id = $previousMetrics.turn_id
        }
        $previousLvaMetrics = Get-LatestLvaCaptureMetrics
        if ($null -ne $previousLvaMetrics) {
            $record.previous_lva_metrics_key = "$($previousLvaMetrics.process_instance_id):$($previousLvaMetrics.capture_sequence)"
            $record.previous_lva_process_instance_id = $previousLvaMetrics.process_instance_id
            $record.previous_lva_capture_sequence = $previousLvaMetrics.capture_sequence
        }

        $client = [System.Net.Http.HttpClient]::new()
        $client.DefaultRequestHeaders.Authorization = [System.Net.Http.Headers.AuthenticationHeaderValue]::new('Bearer', $token)
        $body = @{ entity_id = $EntityId; question_media_id = $SilentMediaId; preannounce = $false } | ConvertTo-Json -Compress
        $content = [System.Net.Http.StringContent]::new($body, [Text.Encoding]::UTF8, 'application/json')
        $requestStarted = [DateTimeOffset]::UtcNow
        $record.request_started_utc = $requestStarted.ToString('o')
        $task = $client.PostAsync("$haUrl/api/services/assist_satellite/ask_question?return_response", $content)
        $requestCreated = $true

        $listening = $false
        $listenDeadline = [DateTime]::UtcNow.AddSeconds(10)
        while ([DateTime]::UtcNow -lt $listenDeadline) {
            $state = Invoke-HaGet $stateUri $headers
            $changed = [DateTimeOffset]::Parse($state.last_changed)
            if ($state.state -eq 'listening' -and $changed -ge $requestStarted) {
                $listening = $true
                $listeningObserved = [DateTimeOffset]::UtcNow
                $record.listening_observed_utc = $listeningObserved.ToString('o')
                break
            }
            Start-Sleep -Milliseconds 100
        }
        if (-not $listening) { throw 'Fresh listening state was not observed' }

        Start-Sleep -Milliseconds 100
        Assert-BeforeQuietDeadline ($phraseDuration + 1)
        $record.play_started_local = (Get-YekaterinburgNow).ToString('o')
        & ffplay.exe -nodisp -autoexit -loglevel error -volume $level -- $PhraseWav
        if ($LASTEXITCODE -ne 0) { throw "ffplay failed at level $level" }

        if (-not $task.Wait([TimeSpan]::FromSeconds(35))) { throw "ask_question timed out at level $level" }
        $response = $task.GetAwaiter().GetResult()
        $requestCompleted = [DateTimeOffset]::UtcNow
        $record.request_completed_utc = $requestCompleted.ToString('o')
        $record.http_status = [int]$response.StatusCode
        $responseBody = $response.Content.ReadAsStringAsync().GetAwaiter().GetResult()
        try {
            $record.stt_response = ($responseBody | ConvertFrom-Json)
        }
        catch {
            $record.stt_response = $responseBody
        }
        if (-not $response.IsSuccessStatusCode) {
            throw "ask_question returned HTTP $([int]$response.StatusCode)"
        }
    }
    catch {
        Add-RecordError $record $_.Exception.Message
    }
    finally {
        if ($null -ne $task -and -not $task.IsCompleted -and $null -ne $client) {
            $client.CancelPendingRequests()
        }

        if ($requestCreated) {
            $record.metrics_status = 'missing_or_stale'
            $metricsDeadline = [DateTime]::UtcNow.AddSeconds(10)
            do {
                $metrics = Get-LatestSttMetrics
                if ($null -ne $metrics -and $metrics.captured_at -and (Test-GeminiMetricsPayload $metrics)) {
                    try {
                        $capturedAt = [DateTimeOffset]::Parse($metrics.captured_at)
                        $newTurn = (-not $record.previous_metrics_turn_id) -or ($metrics.turn_id -ne $record.previous_metrics_turn_id)
                        $timeValid = (
                            $null -ne $requestCompleted -and
                            $capturedAt -ge $requestStarted -and
                            $capturedAt -le $requestCompleted.AddSeconds(2)
                        )
                        if ($timeValid -and $newTurn) {
                            $record.stt_metrics = $metrics
                            $record.metrics_status = 'fresh'
                            $acceptedMetricsCapturedAt = $capturedAt
                            break
                        }
                    }
                    catch {
                        $record.metrics_status = 'invalid'
                    }
                }
                elseif ($null -ne $metrics) {
                    $record.metrics_status = 'invalid'
                }
                Start-Sleep -Milliseconds 250
            } while ([DateTime]::UtcNow -lt $metricsDeadline)
            if ($record.metrics_status -ne 'fresh') {
                Add-RecordError $record 'Latest STT metrics are missing, stale, invalid, or from the previous turn'
            }

            $record.lva_metrics_status = 'missing_or_stale'
            $lvaMetricsDeadline = [DateTime]::UtcNow.AddSeconds(10)
            do {
                $lvaMetrics = Get-LatestLvaCaptureMetrics
                if ($null -ne $lvaMetrics -and $lvaMetrics.captured_at -and (Test-LvaMetricsPayload $lvaMetrics)) {
                    try {
                        $lvaStartedAt = [DateTimeOffset]::Parse($lvaMetrics.started_at)
                        $lvaCapturedAt = [DateTimeOffset]::Parse($lvaMetrics.captured_at)
                        $lvaKey = "$($lvaMetrics.process_instance_id):$($lvaMetrics.capture_sequence)"
                        $boundaryMs = [double]$lvaMetrics.boundary_tolerance_ms
                        $schemaValid = [int]$lvaMetrics.schema_version -eq 1
                        $reasonValid = $lvaMetrics.reason -eq 'streaming_ended'
                        $settingsStable = -not [bool]$lvaMetrics.settings_changed
                        $timeValid = (
                            $null -ne $listeningObserved -and
                            $null -ne $requestCompleted -and
                            $boundaryMs -ge 0 -and $boundaryMs -le 1000 -and
                            $lvaStartedAt -ge $requestStarted.AddMilliseconds(-$boundaryMs) -and
                            $lvaStartedAt -le $listeningObserved.AddSeconds(1).AddMilliseconds($boundaryMs) -and
                            $lvaCapturedAt -ge $lvaStartedAt -and
                            $lvaCapturedAt -le $requestCompleted.AddSeconds(2)
                        )
                        $processStable = (
                            -not $record.previous_lva_process_instance_id -or
                            $lvaMetrics.process_instance_id -eq $record.previous_lva_process_instance_id
                        )
                        $sequenceExact = (
                            $null -eq $record.previous_lva_capture_sequence -or
                            [int]$lvaMetrics.capture_sequence -eq ([int]$record.previous_lva_capture_sequence + 1)
                        )
                        if (-not $processStable) {
                            $record.lva_metrics_status = 'process_changed'
                            break
                        }
                        if (-not $sequenceExact) {
                            $record.lva_metrics_status = 'sequence_ambiguous'
                            break
                        }
                        if (-not $schemaValid -or -not $reasonValid -or -not $settingsStable -or -not $timeValid) {
                            $record.lva_metrics_status = 'invalid_window'
                            break
                        }
                        if ($lvaCapturedAt -ge $requestStarted -and $lvaKey -ne $record.previous_lva_metrics_key) {
                            $record.lva_capture_metrics = $lvaMetrics
                            $record.lva_metrics_status = 'fresh'
                            $acceptedLvaStartedAt = $lvaStartedAt
                            $acceptedLvaCapturedAt = $lvaCapturedAt
                            $acceptedLvaBoundaryMs = $boundaryMs
                            break
                        }
                    }
                    catch {
                        $record.lva_metrics_status = 'invalid'
                    }
                }
                elseif ($null -ne $lvaMetrics) {
                    $record.lva_metrics_status = 'invalid_payload'
                }
                Start-Sleep -Milliseconds 250
            } while ([DateTime]::UtcNow -lt $lvaMetricsDeadline)
            if ($record.lva_metrics_status -ne 'fresh') {
                Add-RecordError $record "Latest LVA capture metrics rejected: $($record.lva_metrics_status)"
            }
            if ($record.metrics_status -eq 'fresh' -and $record.lva_metrics_status -eq 'fresh') {
                $pairTimeValid = (
                    $acceptedMetricsCapturedAt -ge $acceptedLvaStartedAt.AddMilliseconds(-$acceptedLvaBoundaryMs) -and
                    $acceptedLvaCapturedAt -le $requestCompleted.AddSeconds(2)
                )
                if (-not $pairTimeValid) {
                    $record.metrics_status = 'lva_time_mismatch'
                    $record.lva_metrics_status = 'gemini_time_mismatch'
                    Add-RecordError $record 'Gemini and LVA metrics do not fit the same request window'
                }
            }

            try {
                $record.satellite_idle = Wait-SatelliteIdle $stateUri $headers 15
                if (-not $record.satellite_idle) {
                    Add-RecordError $record "Satellite did not return to idle after level $level"
                }
            }
            catch {
                $record.satellite_idle = $false
                Add-RecordError $record "Idle check failed: $($_.Exception.Message)"
            }
            try {
                $postAttemptAudio = Get-HaAudioSnapshot
                $postAttemptInput = $postAttemptAudio.DefaultInput
                $record.post_attempt_lva_streams = $postAttemptAudio.VoiceStreamCount
                $record.post_attempt_input_name = $postAttemptInput.name
                $record.post_attempt_input_volume_percent = [Math]::Round([double]$postAttemptInput.volume * 100, 1)
                $record.post_attempt_input_muted = [bool]$postAttemptInput.mute
                $record.post_attempt_audio_healthy = (
                    $postAttemptInput.name -eq $defaultInput.name -and
                    [Math]::Abs([double]$postAttemptInput.volume - [double]$defaultInput.volume) -le 0.0001 -and
                    [bool]$postAttemptInput.mute -eq [bool]$defaultInput.mute
                )
                if (-not $record.post_attempt_audio_healthy) {
                    Add-RecordError $record 'Post-attempt HA Audio input drifted from baseline'
                }
            }
            catch {
                $record.post_attempt_audio_healthy = $false
                Add-RecordError $record "Post-attempt HA Audio snapshot failed: $($_.Exception.Message)"
            }
        }

        if ($null -ne $response) { $response.Dispose() }
        if ($null -ne $content) { $content.Dispose() }
        if ($null -ne $client) { $client.Dispose() }

        if (-not $record.error -and $record.metrics_status -eq 'fresh') {
            if ($record.stt_metrics.outcome -eq 'stt_success') {
                $record.status = 'transcript_present'
            }
            else {
                $record.status = 'stt_failed'
                Add-RecordError $record "STT outcome was $($record.stt_metrics.outcome)"
                $record.continuation_safe = (
                    $record.satellite_idle -and
                    $record.post_attempt_audio_healthy -and
                    $null -ne $record.http_status -and
                    $record.http_status -ge 200 -and
                    $record.http_status -lt 300
                )
            }
        }
        Add-Content -LiteralPath $resultsFullPath -Encoding utf8 -Value ($record | ConvertTo-Json -Depth 10 -Compress)
    }

    if ($record.status -eq 'failed' -or ($record.status -eq 'stt_failed' -and -not $record.continuation_safe)) {
        throw "Calibration aborted after unsafe failure at level ${level}, attempt ${attemptIndex}: $($record.error)"
    }
    if ($record.status -eq 'stt_failed' -and $StopAfterFailure) {
        throw "Calibration stopped after level ${level}, attempt ${attemptIndex}: $($record.error)"
    }
    }
}
