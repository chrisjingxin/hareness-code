import { EventEmitter } from "events";
import type { AudioStats } from "./zig-structs.js";
export interface AudioSetupOptions {
    autoStart?: boolean;
    sampleRate?: number;
    playbackChannels?: number;
    startOptions?: AudioStartOptions;
}
export interface AudioStartOptions {
    periodSizeInFrames?: number;
    periodSizeInMilliseconds?: number;
    periods?: number;
    performanceProfile?: number;
    shareMode?: number;
    noPreSilencedOutputBuffer?: boolean;
    noClip?: boolean;
    noDisableDenormals?: boolean;
    noFixedSizedCallback?: boolean;
    wasapiNoAutoConvertSrc?: boolean;
    wasapiNoDefaultQualitySrc?: boolean;
    alsaNoMMap?: boolean;
    alsaNoAutoFormat?: boolean;
    alsaNoAutoChannels?: boolean;
    alsaNoAutoResample?: boolean;
}
export interface AudioPlayOptions {
    volume?: number;
    pan?: number;
    loop?: boolean;
    groupId?: number;
}
export type AudioGroup = number;
export type AudioVoice = number;
export type AudioSound = number;
export interface AudioPlaybackDevice {
    index: number;
    name: string;
    isDefault: boolean;
}
export type AudioAction = "createAudioEngine" | "start" | "startMixer" | "stop" | "loadSound" | "loadSoundFile" | "unloadSound" | "group" | "play" | "stopVoice" | "setVoiceGroup" | "setGroupVolume" | "setMasterVolume" | "mixFrames" | "enableTap" | "readTapFrames" | "listPlaybackDevices" | "selectPlaybackDevice" | "clearPlaybackDeviceSelection" | "getStats";
export interface AudioErrorContext {
    action: AudioAction;
    status?: number;
}
export interface AudioEvents {
    error: [error: Error, context: AudioErrorContext];
    started: [];
    mixerStarted: [];
    stopped: [];
    disposed: [];
}
export declare class Audio extends EventEmitter<AudioEvents> {
    static create(options?: AudioSetupOptions): Audio;
    private readonly lib;
    private readonly defaultStartOptions;
    private engine;
    private readonly groups;
    private playbackStarted;
    private mixerStarted;
    private constructor();
    private emitError;
    start(options?: AudioStartOptions): boolean;
    startMixer(): boolean;
    stop(): boolean;
    isStarted(): boolean;
    isMixerStarted(): boolean;
    loadSound(data: Uint8Array | ArrayBuffer): AudioSound | null;
    loadSoundFile(filePath: string): Promise<AudioSound | null>;
    unloadSound(sound: AudioSound): boolean;
    group(name: string): AudioGroup | null;
    play(sound: AudioSound, options?: AudioPlayOptions): AudioVoice | null;
    stopVoice(voice: AudioVoice): boolean;
    setVoiceGroup(voice: AudioVoice, group: AudioGroup): boolean;
    setGroupVolume(group: AudioGroup, volume: number): boolean;
    setMasterVolume(volume: number): boolean;
    mixFrames(frameCount: number, channels?: number): Float32Array | null;
    enableTap(capacityFrames?: number): boolean;
    disableTap(): boolean;
    readTapFrames(frameCount: number, channels?: number): {
        frames: Float32Array;
        framesRead: number;
    } | null;
    listPlaybackDevices(): AudioPlaybackDevice[] | null;
    selectPlaybackDevice(index: number): boolean;
    clearPlaybackDeviceSelection(): void;
    getStats(): AudioStats | null;
    dispose(): void;
}
export declare function setupAudio(options?: AudioSetupOptions): Audio;
