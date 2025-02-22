import pysam
from distutils import spawn
import sys
import subprocess
import os
import re
from .variant import Variant, Allele
from collections import defaultdict, Counter
from operator import itemgetter, attrgetter
import itertools
from .utils import split_tasks, parallel_process, combine_batch_results, create_tmp_file, reverse_complement, merge_spans, complement_spans
from .ins import INSFinder, INS
import math
import random
from pybedtools import BedTool
from datetime import datetime

class TREFinder:
    def __init__(self, bam, genome_fasta, sex="female", reads_fasta=None, check_split_alignments=True,
                 max_str_len=50, min_str_len=2, flank_size=100, min_support=2, nprocs=1,
                 max_num_clusters=3, min_cluster_size=2,
                 genotype_in_size=False, trf_args='2 5 5 80 10 10 500 -d -h', debug=False):
        self.bam = bam
        self.genome_fasta = genome_fasta
        self.sex = sex
        trf_path = spawn.find_executable("trf")
        if not trf_path:
            sys.exit('ABORT: {}'.format("can't find trf in PATH"))

        self.trf_args = trf_args
        self.flank_len = 2000

        self.reads_fasta = reads_fasta

        # for checking sequences flanking repeat
        self.trf_flank_min_mapped_fraction = 0.7
        self.trf_flank_size = flank_size
        self.nprocs = nprocs

        self.check_split_alignments = check_split_alignments

        self.update_loci = True

        self.max_str_len = max_str_len
        self.min_str_len = min_str_len

        self.min_support = min_support
        self.min_cluster_size = min_cluster_size

        self.tmp_files = set()

        # Cluster object for genotyping
        self.max_num_clusters = max_num_clusters

        # report genotype in size instead of copy numbers (default)
        self.genotype_in_size = genotype_in_size

        # True when running in genotyping mode - strictly genotyping within given coordinates
        self.strict = False

        self.debug = debug
        self.remove_tmps = True if not self.debug else False

    def construct_trf_output(self, input_fasta):
        m = re.search('(\d[\d\s]*\d)', self.trf_args)
        if m is not None:
            return '{}/{}.{}.dat'.format(os.getcwd(), os.path.basename(input_fasta), m.group(1).replace(' ', '.'))

    def run_trf(self, input_fasta):
        cmd = ' '.join(['trf', input_fasta, self.trf_args])
        # redirect stdout and stderr to devnull
        FNULL = open(os.devnull, 'w')
        returncode = subprocess.call(cmd, shell=True, stdout=FNULL, stderr=FNULL)

        output = self.construct_trf_output(input_fasta)
        if os.path.exists(output):
            return output
        else:
            sys.exit('cannot run {}'.format(cmd))

    def type_trf_cols(self, cols):
        return list(map(int, cols[:3])) + [float(cols[3])] + list(map(int, cols[4:12])) + [float(cols[12])] + cols[13:]

    def parse_trf(self, trf_output):
        """
            columns:
            start_index, end_index, periold_size, copy_number, consensus_size,
            percent_matches, percent_index, score, A, C, G, T, entropy,
            consensus_pattern

            start and end index are 1-based
        """
        self.tmp_files.add(trf_output)
        results = defaultdict(list)
        with open(trf_output, 'r') as ff:
            for line in ff:
                cols = line.rstrip().split()
                if not cols:
                    continue
                if cols[0] == 'Sequence:':
                    seq = cols[1]
                elif len(cols) == 15:
                    if len(cols[13]) >= self.min_str_len and len(cols[13]) <= self.max_str_len:
                        results[seq].append(self.type_trf_cols(cols))

        return results

    def extract_tres(self, ins_list, target_flank=3000):
        genome_fasta = pysam.Fastafile(self.genome_fasta)

        # prepare input for trf
        trf_input = ''
        for ins in ins_list:
            target_start, target_end = ins[1] - target_flank + 1, ins[1] + target_flank
            prefix = '.'.join(map(str, [INS.eid(ins), len(ins[5]), target_start, target_end]))
            trf_input += '>{}.q\n{}\n'.format(prefix, ins[7])
            trf_input += '>{}.t\n{}\n'.format(prefix, self.extract_genome_neighbour(ins[0],
                                                                                    ins[1],
                                                                                    target_flank,
                                                                                    genome_fasta))
            trf_input += '>{}.i\n{}\n'.format(prefix, ins[5])

        results = self.perform_trf(trf_input)

        grouped_results = {}
        for seq in results.keys():
            read, read_type = seq.rsplit('.', 1)
            if not read in grouped_results:
                grouped_results[read] = {}
            grouped_results[read][read_type] = results[seq]

        expansions = self.analyze_trf(grouped_results, target_flank)

        if self.remove_tmps:
            self.cleanup()

        return expansions

    def extract_genome_neighbour(self, chrom, tpos, w, genome_fasta):
        return genome_fasta.fetch(chrom, max(0, tpos - w), tpos + w)

    def analyze_trf(self, results, target_flank, full_cov=0.7):
        same_pats = self.find_similar_long_patterns_ins(results)

        expansions = {}
        for seq_id in sorted(results.keys()):
            eid, ins_len, gstart, gend = seq_id.rsplit('.', 3)

            if 't' in results[seq_id]:
                pat, pgstart, pgend = self.analyze_trf_per_seq(results[seq_id], int(ins_len), int(gstart), int(gend), same_pats, target_flank, full_cov=full_cov, seq_id=seq_id)
                if pat is not None:
                    expansions[eid] = pat, pgstart, pgend

        return expansions

    def find_similar_long_patterns_ins(self, results, min_len=4):
        queries = set()
        targets = set()
        for seq_id in results.keys():
            for seq_type in results[seq_id].keys():
                for result in results[seq_id][seq_type]:
                    if len(result[13]) >= min_len:
                        if seq_type == 'i':
                            queries.add(result[13])
                        else:
                            targets.add(result[13])

        same_pats = {}
        if queries and targets:
            blastn_out = self.align_patterns(queries, targets)
            if blastn_out and os.path.exists(blastn_out):
                same_pats = self.parse_pat_blastn(blastn_out)

        return same_pats

    def is_same_repeat(self, reps, same_pats=None, min_fraction=0.5):
        def check_same_pats(rep1, rep2):
            if rep1 in same_pats:
                if rep2 in same_pats[rep1]:
                    return True

                for rep in same_pats[rep1]:
                    if rep2 in rep:
                        if float(rep.count(rep2) * len(rep2)) / len(rep) >= min_fraction:
                            return True
                    elif rep in rep2:
                        if float(rep2.count(rep) * len(rep)) / len(rep2) >= min_fraction:
                            return True

            return False

        if len(reps[0]) <= len(reps[1]):
            rep1, rep2 = reps[0], reps[1]
        else:
            rep2, rep1 = reps[0], reps[1]

        if rep1 == rep2:
            return True

        perms1 = []
        for i in range(len(rep1)):
            pat = rep1[i:] + rep1[:i]
            perms1.append(pat)

        for p1 in perms1:
            if p1 in rep2:
                if float(rep2.count(p1) * len(p1)) / len(rep2) >= min_fraction:
                    return True

        if same_pats:
            if check_same_pats(reps[0], reps[1]) or check_same_pats(reps[1], reps[0]):
                return True

            for i in range(0, len(reps[0]), 5):
                pat = reps[0][i:] + reps[0][:i]
                if check_same_pats(pat, reps[1]) or check_same_pats(reps[1], pat):
                    return True

            for i in range(0, len(reps[1]), 5):
                pat = reps[1][i:] + reps[1][:i]
                if check_same_pats(pat, reps[0]) or check_same_pats(reps[0], pat):
                    return True

        return False

    def combine_trf_coords(self, coords, bounds, buf=20, max_sep=50):
        # screen out repeat completely in the flanks
        coords = [c for c in coords if not (c[1] < bounds[0] - buf or c[0] > bounds[1] + buf)]
        if not coords:
            return []

        merged_list = merge_spans(coords)
        gap_list = complement_spans(merged_list)

        gaps_filled = []
        for i in range(len(merged_list)-1):
            if gap_list[i]:
                gap_size = gap_list[i][1] - gap_list[i][0] + 1
                if gap_size <= max_sep:
                    gaps_filled.append(gap_list[i])

        return merge_spans(merged_list + gaps_filled)

    def analyze_trf_per_seq(self, result, ins_len, gstart, gend, same_pats, target_flank, full_cov=0.8, mid_pt_buf=50, seq_id=None):
        pattern_matched = None
        pgstart, pgend = None, None

        # filter by locations
        filtered_results = {'i':[], 'q':[], 't':[]}
        filtered_patterns = {'i':[], 'q':[], 't':[]}
        if 'i' in result:
            filtered_results['i'] = [r for r in result['i'] if float(r[1] - r[0] + 1) / ins_len >= full_cov]
            filtered_patterns['i'] = [r[13] for r in filtered_results['i']]

        mid_pts = target_flank + mid_pt_buf, target_flank + 1 - mid_pt_buf
        for pt in ('q', 't'):
            if pt in result:
                filtered_results[pt] = [r for r in result[pt] if r[0] <= mid_pts[0] and r[1] >= mid_pts[1]]
                filtered_patterns[pt] = [r[13] for r in filtered_results[pt]]

        for i_result in sorted(filtered_results['i'], key=lambda r: len(r[13])):
            i_pat = i_result[13]
            if len(i_pat) == 1 or len(set(i_pat)) == 1:
                continue
            i_pat_matches = {'q': False, 't': False}
            for pt in ('t', 'q'):
                for r in sorted(filtered_results[pt], key=lambda r: len(r[13]), reverse=True):
                    pat = r[13]
                    if i_pat in pat or pat in i_pat or self.is_same_repeat((i_pat, pat), same_pats):
                        i_pat_matches[pt] = True
                        break

            if i_pat_matches['q'] or i_pat_matches['t']:
                pattern_matched = i_pat

                if i_pat_matches['t']:
                    dist_from_mid = {}
                    rep_lens = {}
                    for i in range(len(filtered_results['t'])):
                        result = filtered_results['t'][i]
                        dist_from_mid[i] = min(abs(float(result[0]) - target_flank), abs(float(result[1]) - target_flank))
                        rep_lens[i] = len(result[-1])

                    rep_len = None
                    # check for identical repeats first
                    for i in range(len(filtered_results['t'])):
                        r = filtered_results['t'][i]
                        if len(set(r[13])) > 1 and self.is_same_repeat((i_pat, r[13]), min_fraction=1):
                            if rep_len is None or rep_lens[i] > rep_len:
                               pgstart, pgend = gstart + r[0] - 1, gstart + r[1] - 1
                               rep_len = rep_lens[i]
                               if r[13] != i_pat:
                                    pattern_matched += ',{}'.format(r[13])

                    if pgstart is None:
                        for i in range(len(filtered_results['t'])):
                            r = filtered_results['t'][i]
                            if len(set(r[13])) == 1:
                                continue
                            if i_pat in r[13] or r[13] in i_pat or self.is_same_repeat((i_pat, r[13]), same_pats):
                                if rep_len is None or rep_lens[i] > rep_len:
                                    pgstart, pgend = gstart + r[0] - 1, gstart + r[1] - 1
                                    rep_len = rep_lens[i]
                                    if r[13] != i_pat:
                                        pattern_matched += ',{}'.format(r[13])

                break

        # reference and query(flanking) may not have the repeat long enough for trf to detect
        if not pattern_matched and filtered_patterns['i']:
            # if target patterns don't match, but there is, just use longest one
            if filtered_results['t']:
                tpats = [r for r in filtered_results['t'] if int(r[0]) < target_flank and int(r[1]) > target_flank]
                if tpats:
                    tpats_sorted = sorted(tpats, key=lambda p:len(p[-1]), reverse=True)
                    pgstart, pgend = gstart + tpats_sorted[0][0] - 1, gstart + tpats_sorted[0][1] - 1
            if not pgstart and not pgend:
                # just take the pattern with the longest repeat
                candidates = sorted([r for r in filtered_results['i'] if r[13] in filtered_patterns['i']], key=lambda r:len(r[-1]), reverse=True)
                pattern_matched = candidates[0][13]
                # deduce the insertion pos from gstart and gend, and use it as the repeat insertion point
                mid = float(gstart + gend) / 2
                pgstart, pgend = int(math.floor(mid)), int(math.ceil(mid))

        return pattern_matched, pgstart, pgend

    def annotate(self, ins_list, expansions):
        ins_dict = dict((INS.eid(ins), ins) for ins in ins_list)
        for eid in expansions.keys():
            if eid in ins_dict:
                ins_dict[eid][6] = 'tre'
                ins_dict[eid].append(expansions[eid][0])

                if expansions[eid][1] is not None and expansions[eid][2] is not None:
                    ins_dict[eid][1] = expansions[eid][1]
                    ins_dict[eid][2] = expansions[eid][2]

    def merge_loci(self, tres, d=100):
        bed_line = ''
        for tre in tres:
            for motif in tre[8].split(','):
                bed_line += '{}\n'.format('\t'.join(map(str, [tre[0], tre[1], tre[2], motif])))
        tres_bed = BedTool(bed_line, from_string=True)
        tres_merged = tres_bed.sort().merge(d=d, c='4,2', o='distinct,count')
        if self.debug:
            tres_merged.saveas('tres_loci.bed')

        merged = []
        for tre in tres_merged:
            repeats = sorted(tre[3].split(','), key=len)
            if len(repeats[0]) < self.min_str_len:
                continue
            merged.append([tre[0], int(tre[1]), int(tre[2]), tre[3]])

        return merged

    def extract_aln_tuple(self, aln, tcoord, search_direction, max_extend=200):
        found = []
        tpos = tcoord
        if search_direction == 'left':
            while not found and tpos >= tcoord - max_extend:
                found = [p for p in aln.aligned_pairs if p[1] == tpos and p[0] is not None]
                tpos -= 1
        else:
            while not found and tpos <= tcoord + max_extend:
                found = [p for p in aln.aligned_pairs if p[1] == tpos and p[0] is not None]
                tpos += 1

        if found:
            return found[0]
        else:
            return found

    def extract_subseq(self, aln, target_start, target_end, reads_fasta=None, max_extend=50):
        tstart = None
        tend = None
        qstart = None
        qend = None
        seq = None

        if target_start >= aln.reference_start and target_start <= aln.reference_end:
            aln_tuple = self.extract_aln_tuple(aln, target_start, 'left', max_extend=max_extend)
            if aln_tuple:
                qstart, tstart = aln_tuple

        if target_end >= aln.reference_start and target_end <= aln.reference_end:
            aln_tuple = self.extract_aln_tuple(aln, target_end, 'right', max_extend=max_extend)
            if aln_tuple:
                qend, tend = aln_tuple

        if qstart is not None and qend is not None:
            if aln.cigartuples[0][0] == 5:
                qstart += aln.cigartuples[0][1]
                qend += aln.cigartuples[0][1]

            if not reads_fasta:
                seq = aln.query_sequence[qstart:qend]
            else:
                seq = INSFinder.get_seq(reads_fasta, aln.query_name, aln.is_reverse, [qstart, qend])

        return seq, tstart, tend, qstart

    def create_trf_fasta(self, locus, read, tstart, tend, qstart, seq, read_len):
        """ for genotyping """
        fields = list(locus) + [read, tstart, tend, len(seq), qstart, read_len]
        header = '{}'.format(':'.join(map(str, fields)))

        return header, '>{}\n{}\n'.format(header, seq)

    def examine_repeats(self, seq, repeat, max_sep=100, min_cov=0.8):
        """ for regex extracting, return the most common motif """
        pat = re.compile(repeat.upper().replace('*', '[AGCT]'))
        pat_counts = Counter()
        coords = []
        for m in pat.finditer(seq):
            if not coords or m.start() - coords[-1] - 1 <= max_sep:
                coords.extend([m.start(), m.start() + len(repeat) - 1])
                pattern = seq[m.start():m.start() + len(repeat)]
                pat_counts.update([pattern])
        if not coords:
            return None, None, None
        sorted_coords = sorted(coords)
        return float(sorted_coords[-1] - sorted_coords[0] + 1) / len(seq) >= min_cov,\
               (sorted_coords[0], sorted_coords[-1]),\
               set([pat_counts.most_common()[0][0]])

    def perform_trf(self, seqs):
        trf_fasta = create_tmp_file(seqs)
        if self.debug:
            print('trf input {}'.format(trf_fasta))
        self.tmp_files.add(trf_fasta)
        output = self.run_trf(trf_fasta)
        results = self.parse_trf(output)
        return results

    def find_similar_long_patterns_gt(self, results, patterns, min_len=15, word_size=4):
        same_pats = {}
        queries = defaultdict(set)
        targets = defaultdict(set)
        for seq in results.keys():
            cols = seq.split(':')
            if len(cols) < 7:
                continue
            locus = tuple(cols[:3])
            seq_len = int(cols[-3])
            same_pats[locus] = None
            targets[locus] |= set([s for s in patterns[seq].split(',') if len(s) >= word_size])
            for result in results[seq]:
                if len(result[13]) >= min_len or (seq_len - 2*self.trf_flank_size < 50 and len(patterns[seq]) >= 6 and len(result[13]) >= 0.5 * len(patterns[seq])):
                    queries[locus].add(result[13])

        qseqs = set()
        tseqs = set()
        for locus in queries.keys():
            qseqs |= queries[locus]
        for locus in targets.keys():
            tseqs |= targets[locus]

        same_pats = defaultdict(dict)
        if qseqs and tseqs:
            blastn_out = self.align_patterns(qseqs, tseqs)
            hits = self.parse_pat_blastn(blastn_out)
            if hits:
                for query in hits:
                    for locus in queries.keys():
                        if query in queries[locus]:
                            same_pats[locus][query] = hits[query]

        return same_pats

    def run_blastn_for_missed_clipped(self, query_fa, target_fa, word_size):
        query_file = create_tmp_file(query_fa)
        target_file = create_tmp_file(target_fa)
        blastn_out = create_tmp_file('')
        self.tmp_files.add(query_file)
        self.tmp_files.add(target_file)
        self.tmp_files.add(blastn_out)

        cmd = ' '.join(['blastn',
                        '-query',
                        query_file,
                        '-subject',
                        target_file,
                        '-task blastn -word_size {} -evalue 1e-10 -outfmt 6 -out'.format(word_size),
                        blastn_out])
        # redirect stdout and stderr to devnull
        FNULL = open(os.devnull, 'w')
        returncode = subprocess.call(cmd, shell=True, stdout=FNULL, stderr=FNULL)

        if os.path.exists(blastn_out):
            return blastn_out
        else:
            sys.exit('cannot run {}'.format(cmd))

    def align_patterns(self, queries, targets, locus=None, word_size=4, min_word_size=4):
        query_fa = ''
        min_len = None
        for seq in queries:
            if min_len is None or len(seq) < min_len:
                min_len = len(seq)
            query_fa += '>{}\n{}\n'.format(seq, seq)

        target_fa = ''
        for seq in targets:
            if min_len is None or len(seq) < min_len:
                min_len = len(seq)
            target_fa += '>{}\n{}\n'.format(seq, seq*2)

        if query_fa and target_fa:
            query_file = create_tmp_file(query_fa)
            target_file = create_tmp_file(target_fa)
            blastn_out = create_tmp_file('')
            self.tmp_files.add(query_file)
            self.tmp_files.add(target_file)
            self.tmp_files.add(blastn_out)
            cmd = ' '.join(['blastn',
                            '-query',
                            query_file,
                            '-subject',
                            target_file,
                            '-task blastn -word_size {} -outfmt 6 -perc_identity 80 -qcov_hsp_perc 80 -out'.format(word_size),
                            blastn_out])
            #print(cmd)
            # redirect stdout and stderr to devnull
            FNULL = open(os.devnull, 'w')
            returncode = subprocess.call(cmd, shell=True, stdout=FNULL, stderr=FNULL)

            if os.path.exists(blastn_out):
                return blastn_out
            else:
                sys.exit('cannot run {}'.format(cmd))

    def parse_pat_blastn(self, blastn_out, min_pid=0.8, min_alen=0.8):
        matches = defaultdict(set)
        with open(blastn_out, 'r') as ff:
            for line in ff:
                cols = line.rstrip().split('\t')
                query = cols[0]
                subject = cols[1]
                qlen = len(query)
                slen = len(subject)
                pid = float(cols[2])
                alen = int(cols[3])

                if pid >= min_pid and float(alen)/min(qlen, slen) >= 0.8:
                    matches[query].add(subject)

        return matches

    def extract_alleles_trf(self, trf_input, repeat_seqs, flank, clipped, bam, strands, patterns, reads_fasta, too_far_from_read_end=200):
        results = self.perform_trf(trf_input)
        same_pats = self.find_similar_long_patterns_gt(results, patterns)

        # group by locus
        alleles = defaultdict(dict)

        for seq in results.keys():
            cols = seq.split(':')
            if len(cols) < 7:
                if self.debug:
                    print('problematic seq id: {}'.format(seq))
                continue
            locus = tuple(cols[:3])

            expected_pats = patterns[seq].split(',')
            expected_pat_sizes = [len(p) for p in expected_pats]

            read = ':'.join(cols[3:-5])
            gstart, gend = cols[-5], cols[-4]
            rstart = int(cols[-2])
            read_len = int(cols[-1])

            pat_lens = []
            results_matched = []
            for result in results[seq]:
                if len(set(result[13])) > 1 and len(result[13]) >= self.min_str_len and len(result[13]) <= self.max_str_len:
                    for pat in expected_pats:
                        if self.is_same_repeat((result[13], pat), same_pats=same_pats[locus]):
                            results_matched.append(result)
                            pat_lens.append((result[13], len(result[-1])))
                            continue

            if results_matched:
                seq_len = int(cols[-3])
                bounds = (flank, seq_len - flank)
                combined_coords = self.combine_trf_coords([(r[0], r[1]) for r in results_matched], bounds)
                pat_lens_sorted = sorted(pat_lens, key=itemgetter(1), reverse=True)

                # if coordinates can't be merged pick largest span
                if len(combined_coords) > 1:
                    combined_coords.sort(key=lambda c:c[1]-c[0], reverse=True)

                if combined_coords:
                    check_seq_len = abs(len(repeat_seqs[seq]) - 2 * flank)
                    span = float(combined_coords[0][1] - combined_coords[0][0] + 1)
                    min_span = 0.2 if check_seq_len < 50 else 0.5

                    if combined_coords[0][0] >= (bounds[0] + too_far_from_read_end) or combined_coords[0][1] <= (bounds[1] - too_far_from_read_end):
                        if self.debug:
                            print('too_far_from_read_end', locus, read, combined_coords[0][0], combined_coords[0][1], seq_len, too_far_from_read_end)

                    if check_seq_len == 0 or (span / check_seq_len) < min_span:
                        continue

                    coords = combined_coords[0]
                    repeat_seq = repeat_seqs[seq][coords[0]-1:coords[-1]]
                    size = coords[-1] - coords[0] + 1
                    rpos = rstart + coords[0] - 1
                    #pats = set([r[-2] for r in results_matched])
                    genome_start = int(gstart) + coords[0]
                    genome_end = int(gend) - (seq_len - coords[-1])

                    # pick pat/motif of longest repeat
                    longest_pat_len = pat_lens_sorted[0][1]
                    pats = set([p[0] for p in pat_lens if p[1] == longest_pat_len])

                    # match given coordinates, but coords have to make sense first
                    if self.strict and genome_start < genome_end and size > 50:
                        if genome_start < int(locus[1]):
                            diff = int(locus[1]) - genome_start
                            if diff < size:
                                genome_start = int(locus[1])
                                rpos += diff
                                size -= diff
                        if genome_end > int(locus[2]):
                            diff = genome_end - int(locus[2])
                            if diff < size:
                                genome_end = int(locus[2])
                                size -= diff

                    #print('ff {} {} {} {} {} {} {} {} {} {} {} {}'.format(read, locus, strands[read], size, rpos, gstart, gend, seq_len, coords, genome_start, genome_end, read_len))
                    if not read in alleles[locus]:
                        if strands[read] == '-':
                            rpos = read_len - rpos - size + 1

                        if genome_start < genome_end:
                            alleles[tuple(locus)][read] = (rpos, pats, size, genome_start, genome_end, strands[read])
                        else:
                            alleles[tuple(locus)][read] = (rpos, pats, size, int(gstart), int(gend), strands[read])

        return self.alleles_to_variants(alleles)

    def get_read_seqs(self, headers, bam):
        grouped_reads = defaultdict(list)
        for header in headers:
            chrom, start, end, read = header.split(':')[:4]
            grouped_reads[(chrom, start, end)].append(read)

        read_seqs = {}
        for region, reads in grouped_reads.items():
            for aln in bam.fetch(region[0], int(region[1]), int(region[2])):
                if aln.query_name in reads:
                    read_seqs[aln.query_name] = aln.query_sequence

        return read_seqs

    def extract_alleles_regex(self, headers, repeat_seqs, flank, clipped, bam, strands, patterns, reads_fasta):
        alleles = defaultdict(dict)

        read_seqs = self.get_read_seqs(headers, bam)

        for header in headers:
            cols = header.split(':')
            read = cols[3]
            if not header in repeat_seqs.keys():
                continue
            if not read in read_seqs:
                if self.debug:
                    print('cannot get sequence:{}'.format(read))
                continue
            repeat_seq = repeat_seqs[header][flank:-1*flank]
            repeat = patterns[header]
            has_repeat, rspan, pats = self.examine_repeats(repeat_seq, repeat)
            if has_repeat and pats:
                matched_seq = repeat_seq[rspan[0] : rspan[1] + 1]
                locus = tuple(cols[:3])
                read_seq = read_seqs[read]
                rpos = read_seq.find(matched_seq)
                size = len(matched_seq)
                if strands[read] == '-':
                    rpos = len(read_seq) - rpos - size + 1
                #print('ff {} {} {} {}'.format(read, locus, size, rpos))

                if not read in alleles[locus]:
                    alleles[tuple(locus)][read] = (rpos, pats, size, int(locus[1]), int(locus[2]), strands[read])

        return self.alleles_to_variants(alleles)

    def alleles_to_variants(self, alleles):
        variants = []
        for locus in alleles.keys():
            variant = [locus[0], locus[1], locus[2], [], None, [], '-']

            # check for minimum number of supporting reads
            if len(alleles[locus]) < self.min_support:
                continue

            pat_counts = Counter()
            for read in alleles[locus]:
                variant[3].append([read,
                                   alleles[locus][read][0], # rstart
                                   None, # repeat
                                   None, # copy number
                                   alleles[locus][read][2], # size
                                   alleles[locus][read][3], # genome_start
                                   alleles[locus][read][4], # genome_end
                                   alleles[locus][read][5], # strand
                                   ])

                # update pattern counts
                pat_counts.update(alleles[locus][read][1])

            pat_counts_sorted = pat_counts.most_common()
            top_pats = [pat_counts_sorted[0][0]]
            for i in range(1, len(pat_counts_sorted)):
                if pat_counts_sorted[i][1] == pat_counts_sorted[0][1]:
                    top_pats.append(pat_counts_sorted[i][0])
                else:
                    break
            variant[4] = (sorted(top_pats, key=len)[0])
            for allele in variant[3]:
                allele[2] = variant[4]
                allele[3] = round(float(allele[4]) / len(variant[4]), 1)

            variants.append(variant)

        return variants

    def extract_missed_clipped(self, aln, clipped_end, gpos, min_proportion=0.4, reads_fasta=None):
        clipped_size = None
        if clipped_end == 'start' and aln.cigartuples[0][0] >= 4 and aln.cigartuples[0][0] <= 5:
            clipped_size = aln.cigartuples[0][1]
        elif clipped_end == 'end' and aln.cigartuples[-1][0] >= 4 and aln.cigartuples[-1][0] <= 5:
            clipped_size = aln.cigartuples[-1][1]

        tpos = None
        if clipped_size is not None:
            if clipped_end == 'start':
                qstart, qend = 0, aln.query_alignment_start + self.trf_flank_size
                tup = self.extract_aln_tuple(aln, min(aln.reference_start, gpos[0]) - self.trf_flank_size, 'left')
                if tup:
                    qstart, qend = 0, tup[0]
                    tpos = tup[1]
                return None
            else:
                qstart, qend = aln.query_alignment_end - self.trf_flank_size, aln.infer_read_length()
                tup = self.extract_aln_tuple(aln, max(aln.reference_end, gpos[1]) + self.trf_flank_size, 'right')
                if tup:
                    qstart, qend = tup[0], aln.infer_read_length()
                    tpos = tup[1]
                else:
                    return None

            seq = None
            if not reads_fasta:
                seq = aln.query_sequence[qstart:qend]
            else:
                if aln.cigartuples[0][0] == 5:
                    qstart = aln.cigartuples[0][1] + qstart
                seq = INSFinder.get_seq(reads_fasta, aln.query_name, aln.is_reverse, [qstart, qend])
            return qstart, qend, tpos, seq

        return None

    def get_probe(self, clipped_end, locus, genome_fasta):
        if clipped_end == 'start':
            pend = locus[1] - 1
            pstart = pend - self.trf_flank_size
        else:
            pstart = locus[2]
            pend = pstart + self.trf_flank_size
        pseq = genome_fasta.fetch(locus[0], max(0, pstart), pend)
        return pstart, pend, pseq

    def parse_blastn(self, blastn_out):
        results = []
        with open(blastn_out, 'r') as ff:
            for line in ff:
                cols = line.rstrip().split('\t')
                query = cols[0]
                subject = cols[1]
                pid = float(cols[2])
                alen = int(cols[3])
                qstart = int(cols[6])
                qend = int(cols[7])
                sstart = int(cols[8])
                send = int(cols[9])
                evalue = float(cols[10])
                results.append([query, subject, pid, alen, evalue, qstart, qend, sstart, send])

        if results:
            return sorted(results, key=itemgetter(4))
        else:
            return results

    def rescue_missed_clipped(self, missed, genome_fasta, min_mapped=0.7, max_evalue=1e-10):
        rescued = []
        target_fa = ''
        query_fa = ''
        seqs = {}
        loci = {}
        for locus, clipped_end, read, qstart, qend, tpos, seq in missed:
            target_fa += '>{}:{}:{}:{}:{}\n{}\n'.format(read, qstart, qend, tpos, len(seq), seq)
            seqs[read] = seq
            loci[read] = locus

            pstart, pend, pseq = self.get_probe(clipped_end, locus, genome_fasta)
            query_fa += '>{}:{}:{}:{}:{}\n{}\n'.format(read, clipped_end, pstart, pend, len(pseq), pseq)

        blastn_out = self.run_blastn_for_missed_clipped(query_fa, target_fa, 6)
        if os.path.exists(blastn_out):
            results = self.parse_blastn(blastn_out)
            if results:
                by_read = defaultdict(list)
                # group blastn results
                for r in results:
                    if list(r[0].split(':'))[0] == list(r[1].split(':'))[0]:
                        read = list(r[0].split(':'))[0]
                        by_read[read].append(r)

                for read in sorted(by_read.keys()):
                    # filter results
                    rlen = int(by_read[read][0][0].split(':')[-1])
                    filtered_results = [r for r in by_read[read] if r[3] / rlen >= min_mapped and r[4] <= max_evalue]
                    if not filtered_results:
                        continue
                    best_result = filtered_results[0]
                    read, qstart, qend, tpos, tlen = best_result[1].split(':')
                    read, clipped_end, pstart, pend, plen = best_result[0].split(':')

                    if clipped_end == 'start':
                        qstart = int(qstart) + best_result[-2]
                        tstart = int(pstart)
                        tend = int(tpos)
                        trf_seq = seqs[read][best_result[-2]:]
                    else:
                        qend = int(qstart) + best_result[-1]
                        tstart = int(tpos)
                        tend = int(pend)
                        trf_seq = seqs[read][:best_result[-1]]

                    rescued.append([read, clipped_end, qstart, qend, tstart, tend, trf_seq, loci[read]])

        return rescued

    def get_alleles(self, loci, reads_fasta=[], closeness_to_end=50):
        bam = pysam.Samfile(self.bam, 'rb')
        if self.reads_fasta:
            for fa in self.reads_fasta:
                reads_fasta.append(pysam.Fastafile(fa))
        genome_fasta = pysam.Fastafile(self.genome_fasta)

        trf_input = ''
        strands = {}
        repeat_seqs = {}
        generic = set()
        patterns = {}
        single_neighbour_size = 100
        split_neighbour_size = 500
        min_mapped = 0.5
        all_clipped = {}
        missed_clipped = []

        if self.strict:
            self.trf_flank_size = 80

        for locus in loci:
            clipped = defaultdict(dict)
            alns = []
            for aln in bam.fetch(locus[0], locus[1] - split_neighbour_size, locus[2] + split_neighbour_size):
                if not reads_fasta and not aln.query_sequence:
                    continue
                alns.append(aln)
                strands[aln.query_name] = '-' if aln.is_reverse else '+'
                locus_size = locus[2] - locus[1] + 1

                # check split alignments first
                if self.check_split_alignments and\
                   ((aln.reference_start >= locus[1] - split_neighbour_size and aln.reference_start <= locus[2] + split_neighbour_size) or\
                    (aln.reference_end >= locus[1] - split_neighbour_size and aln.reference_end <= locus[2] + split_neighbour_size)):
                    check_end = None
                    start_olap = False
                    end_olap = False
                    if aln.reference_start >= locus[1] - split_neighbour_size and aln.reference_start <= locus[2] + split_neighbour_size:
                        start_olap = True
                    if aln.reference_end >= locus[1] - split_neighbour_size and aln.reference_end <= locus[2] + split_neighbour_size:
                        end_olap = True
                    if start_olap and not end_olap:
                        check_end = 'start'
                    elif end_olap and not start_olap:
                        check_end = 'end'
                    clipped_end = None
                    if check_end is not None:
                        clipped_end, partner_start = INSFinder.is_split_aln_potential_ins(aln, min_split_size=400, closeness_to_end=10000, check_end=check_end, use_sa=True)
                    if clipped_end is not None:
                        clipped[aln.query_name][clipped_end] = (aln, partner_start)

            # clipped alignment
            remove = set()
            for read in clipped.keys():
                if len(clipped[read].keys()) == 2:
                    aln1 = clipped[read]['end'][0]
                    aln2 = clipped[read]['start'][0]
                    if aln1.is_reverse != aln2.is_reverse:
                        continue
                    if reads_fasta or aln1.query_alignment_end < aln2.query_alignment_start:
                        aln1_tuple = self.extract_aln_tuple(aln1, locus[1] - self.trf_flank_size, 'left')
                        aln2_tuple = self.extract_aln_tuple(aln2, locus[2] + self.trf_flank_size, 'right')
                        qstart = None
                        qend = None
                        tstart = None
                        tend = None
                        if aln1_tuple and aln2_tuple:
                            qstart, tstart = aln1_tuple
                            qend, tend = aln2_tuple
                            if not reads_fasta:
                                seq = aln1.query_sequence[qstart:qend]
                            else:
                                if aln1.cigartuples[0][0] == 5:
                                    qstart = aln1.cigartuples[0][1] + qstart
                                if aln2.cigartuples[0][0] == 5:
                                    qend = aln2.cigartuples[0][1] + qend
                                seq = INSFinder.get_seq(reads_fasta, aln1.query_name, aln1.is_reverse, [qstart, qend])
                            alns.remove(aln1)
                            alns.remove(aln2)

                            if not seq:
                                if self.debug:
                                    print('problem getting seq2 {} {}'.format(aln.query_name, locus))
                                continue
                        else:
                            if aln1.query_alignment_length > aln2.query_alignment_length:
                                clipped_end = 'end'
                                aln = aln1
                            else:
                                clipped_end = 'start'
                                aln = aln2
                            missed = self.extract_missed_clipped(aln, clipped_end, locus[1:], reads_fasta=reads_fasta)
                            if missed:
                                qstart, qend, tpos, seq = missed
                                missed_clipped.append([tuple(locus), clipped_end, read, qstart, qend, tpos, seq])
                            continue

                        # leave patterns out, some too long for trf header
                        header, fa_entry = self.create_trf_fasta(locus[:3], aln1.query_name, tstart, tend, qstart, seq, aln1.infer_read_length())
                        patterns[header] = locus[-1]
                        repeat_seqs[header] = seq

                        if not '*' in locus[-1]:
                            trf_input += fa_entry
                        else:
                            generic.add(header)
                            repeat_seqs[header] = seq

                    else:
                        remove.add(read)
                else:
                    clipped_end = list(clipped[read].keys())[0]
                    aln = clipped[read][clipped_end][0]
                    missed = self.extract_missed_clipped(aln, clipped_end, locus[1:], reads_fasta=reads_fasta)
                    if missed:
                        qstart, qend, tpos, seq = missed
                        missed_clipped.append([tuple(locus), clipped_end, read, qstart, qend, tpos, seq])

            for read in remove:
                del clipped[read]

            all_clipped[tuple(locus)] = clipped

            for aln in alns:
                if aln.reference_start <= locus[1] - single_neighbour_size and\
                   aln.reference_end >= locus[2] + single_neighbour_size:
                    # don't consider alignment if it's deemed split at locus
                    if aln.query_name in clipped:
                        continue
                    seq, tstart, tend, qstart = self.extract_subseq(aln, locus[1] - self.trf_flank_size, locus[2] + self.trf_flank_size, reads_fasta=reads_fasta)
                    if seq is None:
                        if self.debug:
                            print('problem getting seq1 {} {} {} {} {}'.format(aln.query_name, locus, tstart, tend, qstart))
                        continue

                    # leave patterns out, some too long for trf header
                    header, fa_entry = self.create_trf_fasta(locus[:3], aln.query_name, tstart, tend, qstart, seq, aln.infer_read_length())
                    patterns[header] = locus[3]
                    repeat_seqs[header] = seq

                    if not '*' in locus[3]:
                        trf_input += fa_entry
                    else:
                        generic.add(header)

        if missed_clipped:
            rescued = self.rescue_missed_clipped(missed_clipped, genome_fasta)
            for read, clipped_end, qstart, qend, tstart, tend, seq, locus in rescued:
                clipped = all_clipped[locus]
                aln = clipped[read][list(clipped[read].keys())[0]][0]
                # skip split alignment if start or end too close to repeat (with 50bp)
                if not(aln.reference_start + closeness_to_end) <= locus[1] and not (aln.reference_end - closeness_to_end >= locus[2]):
                    continue
                header, fa_entry = self.create_trf_fasta(locus[:3], read, tstart, tend, qstart, seq, aln.infer_read_length())
                patterns[header] = locus[-3]
                repeat_seqs[header] = seq

                if not '*' in locus[3]:
                    trf_input += fa_entry
                else:
                    generic.add(header)


        variants = []
        if trf_input:
            variants.extend(self.extract_alleles_trf(trf_input,
                                                     repeat_seqs,
                                                     self.trf_flank_size,
                                                     clipped,
                                                     bam,
                                                     strands,
                                                     patterns,
                                                     reads_fasta))

        if generic:
            variants.extend(self.extract_alleles_regex(generic,
                                                       repeat_seqs,
                                                       self.trf_flank_size,
                                                       clipped,
                                                       bam,
                                                       strands,
                                                       patterns,
                                                       reads_fasta))

        # set genotyping configuration (class variable)
        Variant.set_genotype_config(min_reads=self.min_cluster_size, sex=self.sex)

        for variant in variants:
            # genotype
            Variant.genotype(variant, report_in_size=self.genotype_in_size)
            Variant.summarize_genotype(variant)

        if self.remove_tmps:
            self.cleanup()

        return variants

    def examine_ins(self, ins_list, min_expansion=0):
        def filter_tres(ins_list, tre_events):
            for ins in ins_list:
                if ins[6] == 'tre':
                    motifs = [m for m in ins[-1].split(',') if len(m) >= self.min_str_len and len(m) <= self.max_str_len]
                    if motifs:
                        ins[-1] = ','.join(set(motifs))
                        tre_events.append(ins)

        if self.nprocs > 1:
            random.shuffle(ins_list)
            batches = list(split_tasks(ins_list, self.nprocs))
            if not batches:
                return []
            batched_results = parallel_process(self.extract_tres, batches, self.nprocs)
            expansions = combine_batch_results(batched_results, type(batched_results[0]))
        else:
            expansions = self.extract_tres(ins_list)

        # label ins
        self.annotate(ins_list, expansions)

        tre_events = []
        filter_tres(ins_list, tre_events)

        if tre_events:
            merged_loci = self.merge_loci(tre_events)
            variants = self.collect_alleles(merged_loci)

            # remove redundant variants
            if variants:
                self.remove_redundants(variants)

                # udpate coordinates
                for variant in variants:
                    Variant.update_coords(variant)

                return [v for v in variants if Variant.above_min_expansion(v, min_expansion, self.min_support)]

        return []

    def remove_redundants(self, variants):
        groups = defaultdict(list)
        for i in range(len(variants)):
            variant = variants[i]
            key = '-'.join(map(str, [variant[0], variant[1], variant[2]]))
            groups[key].append(i)

        remove = []
        for key in groups.keys():
            if len(groups[key]) > 1:
                remove.extend(sorted(groups[key])[1:])

        for i in sorted(remove, reverse=True):
            del variants[i]

    def collect_alleles(self, loci):
        tre_variants = []
        if self.nprocs > 1:
            random.shuffle(loci)
            batches = list(split_tasks(loci, self.nprocs))
            batched_results = parallel_process(self.get_alleles, batches, self.nprocs)
            tre_variants = combine_batch_results(batched_results, type(batched_results[0]))
        else:
            tre_variants = self.get_alleles(loci)

        return tre_variants

    def genotype(self, loci_bed):
        # extract loci (return fake insertions)
        loci = []
        with open(loci_bed, 'r') as ff:
            for line in ff:
                if line[0] == '#':
                    continue
                cols = line.rstrip().split()
                if len(cols) >= 5:
                    self.max_str_len = 10000
                    self.min_str_len = 2
                    #if len(cols[3]) <= self.max_str_len:
                    loci.append((cols[0], int(cols[1]), int(cols[2]), cols[3], cols[4]))
                else:
                    print("Warning: not enough columns in input BED file")

        # use give loci coordinates for reporting
        self.update_loci = False

        self.strict = True

        return self.collect_alleles(loci)

    def output_tsv(self, variants, out_file, cmd=None):
        with open(out_file, 'w') as out:
            if cmd is not None:
                out.write('#{} {}\n'.format(datetime.now().strftime("%Y-%m-%d_%H:%M:%S"), cmd))
            out.write('#{}\n'.format('\t'.join(Variant.tsv_headers + Allele.tsv_headers)))
            for variant in sorted(variants, key=itemgetter(0, 1, 2)):
                if not variant[5]:
                    continue
                variant_cols = Variant.to_tsv(variant)
                for allele in sorted(variant[3], key=itemgetter(3), reverse=True):
                    allele_cols = Allele.to_tsv(allele)
                    out.write('{}\n'.format('\t'.join(variant_cols + allele_cols)))

    def output_bed(self, variants, out_file):
        headers = Variant.bed_headers

        for i in range(self.max_num_clusters):
            for j in ('size', 'copy_number', 'support'):
                headers.append('allele{}:{}'.format(i+1, j))

        with open(out_file, 'w') as out:
            out.write('#{}\n'.format('\t'.join(headers)))
            for variant in sorted(variants, key=itemgetter(0, 1, 2)):
                cols = variant[:3] + [variant[4]]
                sizes = []
                copy_numbers = []
                supports = []

                gt = Variant.get_genotype(variant)
                for allele, support in gt:
                    if type(allele) is str:
                        continue
                    supports.append(support)
                    if self.genotype_in_size:
                        sizes.append(allele)
                        copy_numbers.append(round(allele / len(variant[4]) , 1))
                    else:
                        copy_numbers.append(allele)
                        sizes.append(allele * len(variant[4]))

                for size, copy_number, support in zip(sizes, copy_numbers, supports):
                    cols.extend([size, copy_number, support])

                if len(gt) < self.max_num_clusters:
                    for i in range(self.max_num_clusters - len(gt)):
                        cols.extend(['-'] * 3)

                out.write('{}\n'.format('\t'.join(map(str, cols))))


    def output_vcf(self, variants, out_file, sample, loci_bed):

        # Get repeat/gene names
        loci = {}
        with open(loci_bed, 'r') as ff:
            for line in ff:
                if line[0] == '#':
                    continue
                cols = line.rstrip().split()
                if len(cols) >= 6:
                    loci["{}:{}-{}".format(cols[0], cols[1], cols[2])] = (cols[4], cols[5])


        VCF_headers = ["CHROM", "POS", "ID", "REF", "ALT", "QUAL", "FILTER", "INFO", "FORMAT"] + [sample]
        headers = VCF_headers

        with open(out_file, 'w') as out:
            out.write('##fileformat=VCFv4.1\n')
            out.write('##INFO=<ID=END,Number=1,Type=Integer,Description="End position of the variant">\n')
            out.write('##INFO=<ID=REF,Number=1,Type=Integer,Description="Reference copy number">\n')
            out.write('##INFO=<ID=REPID,Number=1,Type=String,Description="Repeat identifier as specified in the variant catalog">\n')
            out.write('##INFO=<ID=VARID,Number=1,Type=String,Description="Variant identifier as specified in the variant catalog">\n')
            out.write('##INFO=<ID=RL,Number=1,Type=Integer,Description="Reference length in bp">\n')
            out.write('##INFO=<ID=RU,Number=1,Type=String,Description="Repeat unit in the reference orientation">\n')
            out.write('##INFO=<ID=SVTYPE,Number=1,Type=String,Description="Type of structural variant">\n')
            out.write('##FILTER=<ID=LowDepth,Description="The overall locus depth is below 10x or number of reads spanning one or both breakends is below 5">\n')
            out.write('##FILTER=<ID=PASS,Description="All filters passed">\n')
            out.write('##FORMAT=<ID=AD,Number=.,Type=Integer,Description="Allelic depths for the ref and alt alleles in the order listed">\n')
            out.write('##FORMAT=<ID=ADFL,Number=1,Type=String,Description="Number of flanking reads consistent with the allele">\n')
            out.write('##FORMAT=<ID=ADIR,Number=1,Type=String,Description="Number of in-repeat reads consistent with the allele">\n')
            out.write('##FORMAT=<ID=ADSP,Number=1,Type=String,Description="Number of spanning reads consistent with the allele">\n')
            out.write('##FORMAT=<ID=GT,Number=1,Type=String,Description="Genotype">\n')
            out.write('##FORMAT=<ID=LC,Number=1,Type=Float,Description="Locus coverage">\n')
            out.write('##FORMAT=<ID=REPCI,Number=1,Type=String,Description="Confidence interval for REPCN">\n')
            out.write('##FORMAT=<ID=REPCN,Number=1,Type=String,Description="Number of repeat units spanned by the allele">\n')
            out.write('##FORMAT=<ID=SO,Number=1,Type=String,Description="Type of reads that support the allele; can be SPANNING, FLANKING, or INREPEAT meaning that the reads span, flank, or are fully contained in the repeat">\n')
            out.write('##ALT=<ID=STR10,Description="Allele comprised of 10 repeat units">\n')
            out.write('##ALT=<ID=STR2,Description="Allele comprised of 2 repeat units">\n')
            out.write('#{}\n'.format('\t'.join(headers)))
            for variant in sorted(variants, key=itemgetter(0, 1, 2)):
                cols = variant[:3] + [variant[4]]
                sizes = []
                copy_numbers = []
                supports = []
                ci = {}
                for l, m, u in variant[5]:
                    ci[m] = (l, u)
                #print(variant[5])
                gt = Variant.get_genotype(variant)

                for allele, support in gt:
                    if type(allele) is str:
                        continue
                    supports.append(support)
                    if self.genotype_in_size:
                        sizes.append(allele)
                        copy_numbers.append(round(allele / len(variant[4]) , 1))
                    else:
                        copy_numbers.append(allele)
                        sizes.append(allele * len(variant[4]))

                for size, copy_number, support in zip(sizes, copy_numbers, supports):
                    cols.extend([size, copy_number, support])

                chrom = cols[0]
                start_pos = cols[1]
                end_pos = cols[2]

                ref_repeat_length = int(end_pos) - int(start_pos)
                repeat_unit = cols[3]
                ref_allele = repeat_unit[0]
                ref_repeat_count = int(ref_repeat_length / len(repeat_unit))
                repeat_id, variant_id = loci["{}:{}-{}".format(chrom, start_pos, end_pos)]

                allele1_repeat_count = round(cols[5])
                allel1_ci_lower, allel1_ci_upper = ci[cols[5]]
                allel1_support = cols[6]

                homzygous =  len(gt) == 1

                if homzygous:
                    is_ref_allele = abs(allele1_repeat_count - ref_repeat_count) < 1
                    if is_ref_allele:
                        # No variant here
                        pass
                    else:
                        out.write("{}\t{}\t.\t{}\t<STR{}>\t.\tPASS\tSVTYPE=STR;END={};REF={};RL={};RU={};REPID={};VARID={}\tGT:SO:CN:CI:AD_SP:AD_FL:AD_IR\t1/1:SPANNING/SPANNING:{}/{}:{}-{}/{}-{}:{}/{}:0/0:0/0\n".format(chrom, start_pos, ref_allele, allele1_repeat_count, end_pos,  ref_repeat_count, ref_repeat_length, repeat_unit, repeat_id, variant_id, allele1_repeat_count, allele1_repeat_count, round(allel1_ci_lower), round(allel1_ci_upper), round(allel1_ci_lower), round(allel1_ci_upper), allel1_support / 2, allel1_support / 2))

                else:
                    allele2_repeat_count = round(cols[8])
                    allel2_ci_lower, allel2_ci_upper = ci[cols[8]]
                    allel2_support = cols[9]

                    out.write("{}\t{}\t.\t{}\t<STR{}>,<STR{}>\t.\tPASS\tSVTYPE=STR;END={};REF={};RL={};RU={};REPID={};VARID={}\tGT:SO:CN:CI:AD_SP:AD_FL:AD_IR\t1/2:SPANNING/SPANNING:{}/{}:{}-{}/{}-{}:{}/{}:0/0:0/0\n".format(chrom, start_pos, ref_allele, allele1_repeat_count, allele2_repeat_count, end_pos,  ref_repeat_count, ref_repeat_length, repeat_unit, repeat_id, variant_id, allele1_repeat_count, allele2_repeat_count, round(allel1_ci_lower), round(allel1_ci_upper), round(allel2_ci_lower), round(allel2_ci_upper), allel1_support, allel2_support))

    def cleanup(self):
        if self.tmp_files:
            for ff in self.tmp_files:
                if os.path.exists(ff):
                    os.remove(ff)
