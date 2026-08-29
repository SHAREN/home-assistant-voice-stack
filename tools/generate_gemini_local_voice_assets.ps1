param(
    [Parameter(Mandatory = $true)]
    [string]$OutputPath
)

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Speech

$parent = Split-Path -Parent $OutputPath
if (-not (Test-Path -LiteralPath $parent -PathType Container)) {
    New-Item -ItemType Directory -Path $parent -Force | Out-Null
}

$format = [System.Speech.AudioFormat.SpeechAudioFormatInfo]::new(
    16000,
    [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen,
    [System.Speech.AudioFormat.AudioChannel]::Mono
)
$synth = [System.Speech.Synthesis.SpeechSynthesizer]::new()
try {
    $voice = $synth.GetInstalledVoices() |
        Where-Object { $_.Enabled -and $_.VoiceInfo.Culture.Name -eq 'ru-RU' } |
        Select-Object -First 1
    if ($null -eq $voice) {
        throw 'No enabled ru-RU SAPI voice is installed.'
    }
    $synth.SelectVoice($voice.VoiceInfo.Name)
    $synth.Rate = 0
    $synth.Volume = 100
    $synth.SetOutputToWaveFile($OutputPath, $format)
    $synth.Speak('Нет подключения к интернету.')
}
finally {
    $synth.Dispose()
}

$item = Get-Item -LiteralPath $OutputPath
if ($item.Length -le 44) {
    throw "Generated WAV is unexpectedly small: $($item.Length) bytes"
}
Get-FileHash -Algorithm SHA256 -LiteralPath $OutputPath |
    Select-Object Path, Hash
