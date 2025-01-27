#!/usr/bin/env python3
import argparse
import functools
import os
import sys
import time
from datetime import datetime
from typing import Iterable, Tuple, NamedTuple

import mutagen
import numpy

from audio_io import read_audio_info, read_audio_data, TagKey, TrackInfo, get_tag_with_alternatives
from audio_io.audio_io import AudioSourceInfo, AudioData
from audio_metrics import compute_dr
from util.constants import MEASURE_SAMPLE_RATE


def get_log_path(in_path):
    if os.path.isdir(in_path):
        out_path = in_path
    else:
        out_path = os.path.dirname(in_path)
    return os.path.join(out_path, 'dr.txt')


class LogGroup(NamedTuple):
    performers: Iterable[str]
    albums: Iterable[str]
    channels: int
    sample_rate: int
    tracks_dr: Iterable[Tuple[int, float, float, int, str, str]]


def get_group_title(group: LogGroup):
    return f'{", ".join(group.performers)} — {", ".join(group.albums)}'


def format_time(seconds):
    d = divmod
    m, s = d(seconds, 60)
    h, m = d(m, 60)
    if h:
        return f'{h}:{m:02d}:{s:02d}'
    return f'{m}:{s:02d}'


def write_log(write_fun, dr_log_groups: Iterable[LogGroup], average_dr):
    l1 = '-' * 80
    l2 = '=' * 80
    w = write_fun
    w(f"generated by https://github.com/magicgoose/simple_dr_meter\n"
      f"log date: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")
    for group in dr_log_groups:
        group_name = get_group_title(group)

        w(f"{l1}\nAnalyzed: {group_name}\n{l1}\n\nDR         Peak         RMS     Duration Track\n{l1}\n")
        track_count = 0
        for dr, peak, rms, duration_sec, track_name, file_path in group.tracks_dr:
            dr_formatted = f"DR{str(dr).ljust(4)}" if dr is not None else "N/A   "
            w(dr_formatted +
              f"{peak:9.2f} dB"
              f"{rms:9.2f} dB"
              f"{format_time(duration_sec).rjust(10)} "
              f"{track_name}\n")
            track_count += 1
        w(f"{l1}\n\nNumber of tracks:  {track_count}\nOfficial DR value: DR{average_dr}\n\n"
          f"Samplerate:        {group.sample_rate} Hz\nChannels:          {group.channels}\n{l2}\n\n")

def write_tags(dr_log_groups: Iterable[LogGroup]):
    for group in dr_log_groups:
        print(f"writing tags for {get_group_title(group)}...")
        for track in group.tracks_dr:
            path = track[5]
            dr_value = str(track[0])
            mutagen_file = mutagen.File(path, easy=False)
            if isinstance(mutagen_file, mutagen.mp3.MP3):
                mutagen_file.tags.add(mutagen.id3.TXXX(encoding=mutagen.id3.Encoding.UTF8, desc=u"DR", text=dr_value))
            elif isinstance(mutagen_file, mutagen.mp4.MP4):
                mutagen_file["----:com.apple.iTunes:DR"] = mutagen.mp4.MP4FreeForm(dr_value.encode())
            else:
                mutagen_file["DR"] = dr_value
            mutagen_file.save()
    print("DR tags written!")
    return

def flatmap(f, items):
    for i in items:
        yield from f(i)


def make_log_groups(l: Iterable[Tuple[AudioSourceInfo, Iterable[Tuple[int, float, float, int, str, str]]]]):
    import itertools
    grouped = itertools.groupby(l, key=lambda x: (x[0].channel_count, x[0].sample_rate))

    for ((channels, sample_rate), subitems) in grouped:
        subitems = tuple(subitems)
        performers = set(map(lambda x: get_tag_with_alternatives(x[0].tags, TagKey.PERFORMER), subitems))
        albums = set(map(lambda x: get_tag_with_alternatives(x[0].tags, TagKey.ALBUM), subitems))
        tracks_dr = flatmap(lambda x: x[1], subitems)
        yield LogGroup(
            performers=performers,
            albums=albums,
            channels=channels,
            sample_rate=sample_rate,
            tracks_dr=tracks_dr)


def parse_args():
    ap = argparse.ArgumentParser()
    ap.add_argument("input", help='Input file or directory')
    ap.add_argument("--no-log", help='Do not write log (dr.txt), by default a log file is written after analysis',
                    action='store_true')
    ap.add_argument("--keep-precision", help='Do not round values, this also disables log', action='store_true')
    ap.add_argument("--tag", help='Tag the audio files with the computed DR value. ', action='store_true')
    ap.add_argument("--no-resample", help='Do not resample everything to 44.1kHz (unlike the "standard" meter), '
                                          'this also disables log',
                    action='store_true')
    args = sys.argv[1:]
    if args:
        return ap.parse_args(args)
    else:
        ap.print_help()
        return None


def main():
    args = parse_args()
    if not args:
        return

    input_path = os.path.expanduser(args.input)
    input_path = os.path.abspath(input_path)

    should_write_log = \
        not args.no_log \
        and not args.keep_precision \
        and not args.no_resample
    keep_precision = args.keep_precision
    no_resample = args.no_resample
    should_tag = args.tag

    if should_write_log:
        log_path = get_log_path(input_path)
        if os.path.exists(log_path):
            sys.exit('the log file already exists!')

    def track_cb(track_info: TrackInfo, dr):
        dr_formatted = f'DR{dr}' if dr is not None else 'N/A'
        title = get_tag_with_alternatives(track_info.tags, TagKey.TITLE)
        print(f"{track_info.global_index:02d} - {title}: {dr_formatted}")

    time_start = time.time()
    dr_log_items, dr_mean, dr_median = analyze_dr(
        input_path, track_cb, keep_precision, no_resample,
    )
    print(f'Official DR = {dr_mean}, Median DR = {dr_median}')
    print(f'Analyzed all tracks in {time.time() - time_start:.2f} seconds')

    dr_log_items_list = [LogGroup(performers=item.performers, 
                                  albums=item.albums, 
                                  channels=item.channels, 
                                  sample_rate=item.sample_rate,
                                  tracks_dr=[tuple(track) for track in item.tracks_dr]
                                  ) for item in dr_log_items]

    if should_write_log:
        # noinspection PyUnboundLocalVariable
        print(f'writing log to {log_path}')
        with open(log_path, mode='x', encoding='utf8') as f:
            write_log(f.write, dr_log_items_list, dr_mean)
        print('…done')
    else:
        write_log(sys.stdout.write, dr_log_items_list, dr_mean)

    if should_tag:
        write_tags(dr_log_items_list)

    fix_tty()


def fix_tty():
    """I don't know why this is needed, but it is. Otherwise, the terminal may cease to
    accept any keyboard input after this application finishes. Hopefully I will find
    something better eventually."""
    platform = sys.platform.lower()
    if platform.startswith('darwin') or platform.startswith('linux'):
        if os.isatty(sys.stdin.fileno()):
            os.system('stty sane')


def analyze_dr(
        in_path: str,
        track_cb,
        keep_precision: bool,
        no_resample: bool,
):
    audio_info = tuple(read_audio_info(in_path))
    num_files = len(audio_info)
    assert num_files > 0

    import multiprocessing.dummy as mt
    import multiprocessing

    cpu_count = multiprocessing.cpu_count()

    def choose_map_impl(threads, *, chunksize):
        if threads <= 1:
            return map
        pool = mt.Pool(threads)
        return functools.partial(pool.imap_unordered, chunksize=chunksize)

    threads_outer = max(1, min(num_files, cpu_count))
    threads_inner = cpu_count // threads_outer
    map_impl_outer = choose_map_impl(threads_outer, chunksize=1)
    map_impl_inner = choose_map_impl(threads_inner, chunksize=4)

    def analyze_part_tracks(audio_data: AudioData, audio_info_part: AudioSourceInfo, map_impl):
        for track_samples, track_info in zip(audio_data.blocks_generator, audio_info_part.tracks):
            dr_metrics = compute_dr(map_impl, audio_info_part, track_samples, keep_precision)
            yield track_info, dr_metrics

    def analyze_part(map_impl, audio_info_part: AudioSourceInfo):
        ffmpeg_args = []
        ffmpeg_args += [
            '-loglevel', 'fatal',
            '-i', audio_info_part.file_path,
            '-map', '0:a:0',
            '-c:a', 'pcm_f32le',
        ]

        if not no_resample:
            ffmpeg_args += [
                '-ar', str(MEASURE_SAMPLE_RATE),
                # ^ because apparently official meter resamples to 44k before measuring;
                # using default low quality resampling because it doesn't affect measurements and is faster
            ]

        ffmpeg_args += [
            '-f', 'f32le',
            '-',
        ]

        if no_resample:
            sample_rate = audio_info_part.sample_rate
        else:
            sample_rate = MEASURE_SAMPLE_RATE

        audio_data = read_audio_data(audio_info_part,
                                     samples_per_block=3 * sample_rate,
                                     ffmpeg_args=ffmpeg_args,
                                     bytes_per_sample_mono=4,
                                     numpy_sample_type='<f4',
                                     sample_rate=sample_rate)
        return audio_info_part, analyze_part_tracks(audio_data, audio_info_part, map_impl)

    dr_items = []
    dr_log_items = []

    def process_results(audio_info_part, analyzed_tracks):
        nonlocal dr_items
        dr_log_subitems = []
        dr_log_items.append((audio_info_part, dr_log_subitems))
        track_results = []
        for track_info, dr_metrics in analyzed_tracks:
            dr = dr_metrics.dr
            track_results.append((track_info, dr))
            track_cb(track_info, dr)
            if dr:
                dr_items.append(dr)

            duration_seconds = round(dr_metrics.sample_count / MEASURE_SAMPLE_RATE)
            title = get_tag_with_alternatives(track_info.tags, TagKey.TITLE)
            dr_log_subitems.append(
                (dr, dr_metrics.peak, dr_metrics.rms, duration_seconds,
                 f"{track_info.global_index:02d}-{title}", audio_info_part[0]))
        return track_results

    def process_part(map_impl, audio_info_part: AudioSourceInfo):
        audio_info_part, analyzed_tracks = analyze_part(map_impl, audio_info_part)
        return process_results(audio_info_part, analyzed_tracks)

    for x in map_impl_outer(functools.partial(process_part, map_impl_inner), audio_info):
        # noinspection PyUnusedLocal
        for track_result in x:
            pass  # we need to go through all items for the side effects

    if keep_precision:
        dr_mean_rounded = numpy.mean(dr_items)
    else:
        dr_mean_rounded = int(numpy.round(numpy.mean(dr_items)))  # official
    dr_median = numpy.median(dr_items)

    return make_log_groups(dr_log_items), dr_mean_rounded, dr_median


if __name__ == '__main__':
    main()
