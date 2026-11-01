import unittest

from inspect_h264 import continuity


def ue(value):
    value = bin(value+1)[2:]
    return '0'*(len(value)-1)+value


def nal(header, bits):
    bits += '1'
    bits += '0'*(-len(bits)%8)
    return bytes([header])+int(bits, 2).to_bytes(len(bits)//8, 'big')


def headers(width=4):
    sequence = (f'{66:08b}{0:08b}{42:08b}'+ue(0)+ue(width-4)+ue(2)+ue(1)+
                '0'+ue(119)+ue(67)+'1')
    return [nal(0x67, sequence), nal(0x68, ue(0)+ue(0))]


def picture(number, width=4, idr=False, reference=True, first_mb=0):
    header = 0x65 if idr else 0x41 if reference else 0x01
    return nal(header, ue(first_mb)+ue(2 if idr else 0)+ue(0)+f'{number:0{width}b}')


class ContinuityTests(unittest.TestCase):
    def test_whole_missing_reference_picture_is_detected(self):
        stream = headers()+[picture(0, idr=True), picture(1), picture(3), picture(4)]
        result = continuity(stream)
        self.assertEqual(result['reference_steps'], {1: 2, 2: 1})
        self.assertEqual(result['first_discontinuities'][0]['previous'], 1)
        self.assertEqual(result['first_discontinuities'][0]['frame_num'], 3)
        self.assertEqual(result['first_discontinuities'][0]['gaps_allowed'], 0)

    def test_counter_width_comes_from_sps_and_wrap_is_valid(self):
        stream = headers(8)+[picture(254, 8), picture(255, 8), picture(0, 8), picture(1, 8)]
        self.assertEqual(continuity(stream)['reference_steps'], {1: 3})
        self.assertEqual(continuity(stream)['first_discontinuities'], [])

    def test_nonreference_and_additional_slices_do_not_advance_counter(self):
        stream = headers()+[picture(0, idr=True), picture(0, first_mb=12),
                            picture(1, reference=False), picture(1), picture(2)]
        result = continuity(stream)
        self.assertEqual(result['reference_steps'], {1: 2})
        self.assertEqual(result['counts']['additional_slices'], 1)
        self.assertEqual(result['counts']['non_reference_pictures'], 1)

    def test_repeated_headers_preserve_counter_changed_headers_reset_it(self):
        stream = headers()+[picture(0, idr=True), picture(1)]+headers()+[picture(2)]
        stream += headers(8)+[picture(100, 8), picture(101, 8)]
        result = continuity(stream)
        self.assertEqual(result['reference_steps'], {1: 3})
        self.assertEqual(result['counts']['sequence_parameter_changes'], 1)
        self.assertEqual(result['first_discontinuities'], [])

    def test_truncated_headers_are_reported(self):
        result = continuity(headers()+[b'\x41'])
        self.assertEqual(result['counts']['header_errors'], 1)


if __name__ == '__main__':
    unittest.main()
