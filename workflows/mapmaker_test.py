#!/usr/bin/env python3

# Copyright (c) 2019-2021 Simons Observatory.
# Full license can be found in the top level "LICENSE" file.

"""
This script runs a basic mapmaking test.

"""

import os
import sys
import traceback
import argparse

import numpy as np

from astropy import units as u

# Import sotodlib.toast first before toast, since that sets default object names
# to use in toast.
import sotodlib.toast as sotoast

import toast
import toast.ops

from toast.mpi import MPI

import sotodlib.toast.ops as so_ops


# Small patch 10x10 at zero / zero
# >= 50 dets

# Point source with S/N 20
# random field
# Noise
# Atmosphere last



def parse_args(operators, comm):
    """Parse command line arguments
    """
    # Argument parsing
    parser = argparse.ArgumentParser(description="SO mapmaker test")

    parser.add_argument(
        "--hardware", required=False, default=None, help="Input hardware file, trimmed to desired detectors."
    )
    parser.add_argument(
        "--sample_rate", required=False, default=100, help="Sampling rate"
    )
    parser.add_argument(
        "--sky_file", required=False, default=None, help="Input NSIDE=4096 TQU file to scan from."
    )
    parser.add_argument(
        "--out_dir",
        required=False,
        type=str,
        default="out_mapmaker_test",
        help="The output directory",
    )

    args = parser.parse_args()

    return args


def load_schedule(comm, path):
    """Create and load a schedule file."""
    schedule = toast.schedule.GroundSchedule()
    if comm is None or comm.rank == 0:
        if os.path.isfile(path):
            # Already exists, use it
            schedule.read(path)
        else:
            # Create it
            patches = [
                "RISING_SCAN_35,HORIZONTAL,1.00,30.00,150.00,35.00,1500",
                "SETTING_SCAN_35,HORIZONTAL,1.00,210.00,330.00,35.00,1500",
            ]
            sch_opts = [
                "--site-name", "atacama",
                "--telescope", "LAT",
                "--site-lon", " -67:47:10",
                "--site-lat", " -22:57:30",
                "--site-alt", "5200.0",
                "--patch-coord", "C",
                "--el-min", "30.0",
                "--el-max", "70.0",
                "--sun-el-max", "90.0",
                "--sun-avoidance-angle", "0.0",
                "--moon-avoidance-angle", "0.0",
                "--gap-s", "60.0",
                "--gap-small-s", "0.0",
                "--ces-max-time", "1200.0",
                "--boresight-angle-step", "180.0",
                "--boresight-angle-time", "1440.0",
                "--start", "2027-01-01 00:00:00",
                "--stop", "2027-01-01 00:04:00",
                "--out", path,
            ]
            for patch in patches:
                sch_opts.extend(["--patch", patch])
            toast.schedule_sim_ground.run_scheduler(opts=sch_opts)
            # Now load it
            schedule.read(path)
    if comm is not None:
        schedule = comm.bcast(schedule, root=0)
    return schedule


def load_instrument(comm, args, schedule):
    focalplane = sotoast.SOFocalplane(
        hwfile=args.hardware,
        telescope="LAT",
        sample_rate=args.sample_rate * u.Hz,
        comm=comm,
    )

    site = toast.instrument.GroundSite(
        schedule.site_name,
        schedule.site_lat,
        schedule.site_lon,
        schedule.site_alt,
        weather=None,
    )

    telescope = toast.instrument.Telescope(
        focalplane.telescope, focalplane=focalplane, site=site
    )
    return telescope


def main():
    env = toast.utils.Environment.get()
    log = toast.utils.Logger.get()
    gt = toast.timing.GlobalTimers.get()
    gt.start("mapmaker_test main")

    # Get commandline options
    args = parse_args()

    # Get optional MPI parameters
    comm, procs, rank = toast.get_world()

    # Load (and optionally create) our schedule file in the output directory
    schedule_file = os.path.join(args.out_dir, "schedule.txt")
    schedule = load_schedule(comm, schedule_file)

    # Load our instrument model
    telescope = load_instrument(comm, args, schedule)

    # Create the toast communicator.  Use the default of one process
    # group for this test.
    toast_comm = toast.Comm(world=comm)

    # Timer for reporting the progress
    timer = toast.timing.Timer()
    timer.start()

    # Shortcut for the world communicator
    wcomm = toast_comm.world_comm

    # Note on toast operators:  Operators are configured by "traits" from
    # the python traitlets package.  These can be set at construction time
    # or afterwards by just changing the value.  We use both techniques
    # below but either could be used exclusively.

    # Note on "names".  We use default names for most objects within an
    # observation (e.g. "signal", "elevation", "boresight_radec", etc).
    # These defaults can be changed globally- see sotodlib.toast.__init__.py .

    # ======================================================
    # Simulate Data
    # ======================================================

    # Create the (initially empty) data
    data = toast.Data(comm=toast_comm)

    # Simulate the telescope pointing
    sim_ground = toast.ops.SimGround(
        weather="atacama"
    )
    sim_ground.telescope = telescope
    sim_ground.schedule = schedule
    sim_ground.apply(data)
    log.info_rank("Simulated telescope pointing in", comm=wcomm, timer=timer)

    # Apply LAT co-rotation
    corotate_lat = so_ops.CoRotator()
    corotate_lat.apply(data)
    log.info_rank("Applied LAT co-rotation in", comm=wcomm, timer=timer)

    # Construct a "perfect" noise model just from the focalplane parameters
    default_noise = toast.ops.DefaultNoiseModel()
    default_noise.apply(data)
    log.info_rank("Created default noise model in", comm=wcomm, timer=timer)

    # Configure detector pointing in both Az/El and RA/DEC.  We are not expanding
    # pointing- these classes are passed to other operators.

    det_pointing_azel = toast.ops.PointingDetectorSimple(quats="quats_azel")
    det_pointing_azel.boresight = sim_ground.boresight_azel

    det_pointing_radec = toast.ops.PointingDetectorSimple(quats="quats_radec")
    det_pointing_radec.boresight = sim_ground.boresight_radec

    # Create the Elevation modulated noise model
    elevation_noise = toast.ops.ElevationNoise(out_model="el_noise_model")
    elevation_noise.detector_pointing = det_pointing_azel
    elevation_noise.apply(data)
    log.info_rank("Created elevation noise model in", comm=wcomm, timer=timer)

    # Set up the pointing matrix in RA / DEC, and pointing weights in Az / El
    # in case we need that for the atmosphere sim below.
    pixels_radec = toast.ops.PixelsHealpix(
        detector_pointing=det_pointing_radec,
        nside=4096,
    )
    weights_radec = toast.ops.StokesWeights(
        weights="weights_radec", mode="IQU"
    )
    weights_azel = toast.ops.StokesWeights(
        weights="weights_azel", mode="IQU"
    )

    # Scan input map.  This will create the pixel distribution as well, since
    # it does not yet exist.
    scan_map = toast.ops.ScanHealpix(file=args.sky_file)
    scan_map.pixel_pointing = pixels_radec
    scan_map.stokes_weights = weights_radec
    scan_map.apply(data)
    log.info_rank("Simulated sky signal in", comm=wcomm, timer=timer)

    # Simulate atmosphere.  For this test script, we are just simulating one component
    # and excluding the "coarse" large scale component.
    sim_atmosphere = toast.ops.SimAtmosphere(
        lmin_center=0.001 * u.meter,
        lmin_sigma=0.0 * u.meter,
        lmax_center=1.0 * u.meter,
        lmax_sigma=0.0 * u.meter,
        gain=1.0e-4,
        zatm=40000 * u.meter,
        zmax=200 * u.meter,
        xstep=5 * u.meter,
        ystep=5 * u.meter,
        zstep=5 * u.meter,
        nelem_sim_max=10000,
        wind_dist=3000 * u.meter,
        z0_center=2000 * u.meter,
        z0_sigma=0 * u.meter,
    )
    sim_atmosphere.detector_pointing = det_pointing_azel
    # Here is where we could enable a small polarization fraction, in which
    # case we need to specify the Stokes weights in Az/El.
    #sim_atmosphere.polarization_fraction = 0.01
    #sim_atmosphere.detector_weights = weights_azel
    sim_atmosphere.apply(data)
    log.info_rank("Simulated and observed atmosphere in", comm=wcomm, timer=timer)

    # Simulate detector noise
    sim_noise = toast.ops.SimNoise()
    sim_noise.noise_model = elevation_noise.out_model
    sim_noise.apply(data)
    log.info_rank("Simulated detector noise in", comm=wcomm, timer=timer)

    # ======================================================
    # Reduce Data
    # ======================================================

    # Various geometric factors

    h_n = so_ops.Hn(
        pixel_pointing=pixels_radec,
        output_dir=args.out_dir
    )
    h_n.enabled = True # Toggle to False to disable
    h_n.apply(data)
    log.info_rank("Calculated h_n in", comm=wcomm, timer=timer)

    cadence_map = toast.ops.CadenceMap(
        pixel_pointing=pixels_radec,
        output_dir=args.out_dir
    )
    cadence_map.enabled = True # Toggle to False to disable
    cadence_map.apply(data)
    log.info_rank("Calculated cadence map in", comm=wcomm, timer=timer)

    crosslinking = toast.ops.CrossLinking(
        pixel_pointing=pixels_radec,
        output_dir=args.out_dir
    )
    crosslinking.enabled = True # Toggle to False to disable
    crosslinking.apply(data)
    log.info_rank("Calculated crosslinking in", comm=world_comm, timer=timer)

    # Ground (scan synchronous signal) filter.  Remove modes that are poorly
    # constrained by the scanning.  Disable this if the MLMapmaker does
    # something similar internally.

    ground_filter = toast.ops.GroundFilter()
    ground_filter.enabled = True # Toggle to False to disable
    ground_filter.apply(data)
    log.info_rank("Finished ground-filtering in", comm=wcomm, timer=timer)

    # Run ML mapmaker
    mlmapmaker = so_ops.MLMapmaker(comps="TQU")
    mlmapmaker.apply(data)
    log.info_rank("Finished ML map-making in", comm=wcomm, timer=timer)

    # Filter and bin.  Apply a 2D polynomial filter and then make a binned map
    # We could also iteratively solve for other templates here, but skipping
    # that for now.

    polyfilter2D = toast.ops.PolyFilter2D()
    polyfilter2D.apply(data)
    log.info_rank("Finished 2D-poly-filtering in", comm=wcomm, timer=timer)

    binner = toast.ops.BinMap()
    binner.noise_model = elevation_noise.out_model
    binner.pixel_pointing = pixels_radec
    binner.stokes_weights = weights_radec

    mapmaker = toast.ops.MapMaker()
    mapmaker.binning = binner
    # No templates for now (will just do the binning)
    mapmaker.template_matrix = toast.ops.TemplateMatrix(templates=[])
    mapmaker.output_dir = args.out_dir
    mapmaker.apply(data)
    log.info_rank("Finished Toast map-making in", comm=wcomm, timer=timer)

    # Collect optional timing information
    alltimers = toast.timing.gather_timers(comm=toast_comm.comm_world)
    if toast_comm.world_rank == 0:
        out = os.path.join(args.out_dir, "timing")
        toast.timing.dump(alltimers, out)


if __name__ == "__main__":
    world, procs, rank = toast.mpi.get_world()
    with toast.mpi.exception_guard(comm=world):
        main()
