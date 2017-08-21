# -*- coding: utf-8 -*-
# -----------------------------------------------------------------------------
# (C) British Crown Copyright 2017 Met Office.
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
# * Redistributions of source code must retain the above copyright notice, this
#   list of conditions and the following disclaimer.
#
# * Redistributions in binary form must reproduce the above copyright notice,
#   this list of conditions and the following disclaimer in the documentation
#   and/or other materials provided with the distribution.
#
# * Neither the name of the copyright holder nor the names of its
#   contributors may be used to endorse or promote products derived from
#   this software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
# ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE
# LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
# CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
# SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
# INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
# CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
# ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.
"""Module containing Weighted Blend classes."""
import warnings

import numpy as np
import iris
from iris.analysis import Aggregator

from improver.weights import ChooseDefaultWeightsTriangular
from improver.utilities.cube_manipulation import concatenate_cubes


class PercentileBlendingAggregator(object):
    """Class for the percentile blending aggregator

       This class implements the method described by Combining Probabilities
       by Caroline Jones, 2017. This method implements blending in probability
       space.

       The steps are:
           1. At each geographic point in the cube we take the percentile
              threshold's values across the percentile dimensional coordinate.
              We recalculate, using linear interpolation, their probabilities
              in the pdf of the other points across the coordinate we are
              blending over. Thus at each point we have a set of thresholds
              and their corresponding probability values in each of the
              probability spaces across the blending coordinate.
           2. We do a weighted blend across all the probability spaces,
              combining all the thresholds in all the points in the coordinate
              we are blending over. This gives us an array of thresholds and an
              array of blended probailities for each of the grid points.
           3. We convert back to the original percentile values, again using
              linear interpolation, resulting in blended values at each of the
              original percentiles.

       References:
            Combining Probabilities by Caroline Jones, 2017:
            https://github.com/metoppv/improver/files/1128018/
            Combining_Probabilities.pdf
    """

    def __init__(self):
        """
        Initialise class.
        """
        pass

    def __repr__(self):
        """Represent the configured plugin instance as a string."""
        result = ('<PercentileBlendingAggregator>')
        return result

    @staticmethod
    def aggregate(data, axis, arr_percent, arr_weights, perc_dim):
        """ Blend percentile aggregate function to blend percentile data
            along a given axis of a cube.

        Args:
            data : np.array
                   Array containing the data to blend
            axis : integer
                   The index of the coordinate dimension in the cube. This
                   dimension will be aggregated over.
            arr_percent: np.array
                     Array of percentile values e.g
                     [0, 20.0, 50.0, 70.0, 100.0],
                     same size as the percentile dimension of data.
            arr_weights: np.array
                     Array of weights, same size as the axis dimension of data.
            perc_dim : integer
                     The index of the percentile coordinate
            (Note percent and weights have special meaning in Aggregator
             hence the rename.)

        Returns:
            result : np.array
                     containing the weighted percentile blend data across
                     the chosen coord. The dimension associated with axis
                     has been collapsed, and the rest of the dimensions remain.
        """
        # Iris aggregators support indexing from the end of the array.
        if axis < 0:
            axis += data.ndim
        # Firstly ensure axis coordinate and percentile coordinate
        # are indexed as the first and second values in the data array
        data = np.moveaxis(data, [perc_dim, axis], [1, 0])

        # Determine the rest of the shape
        shape = data.shape[2:]
        input_shape = [data.shape[0],
                       data.shape[1],
                       np.prod(shape, dtype=int)]
        # Flatten the data that is not percentile or coord data
        data = data.reshape(input_shape)
        # Create the resulting data array, which is the shape of the original
        # data without dimension we are collapsing over
        result = np.zeros(input_shape[1:])
        # Loop over the flattened data, i.e. across all the data points in
        # each slice of the coordinate we are collapsing over, finding the
        # blended percentile values at each point.
        for i in range(data.shape[-1]):
            result[:, i] = (
                PercentileBlendingAggregator.blend_percentiles(
                    data[:, :, i], arr_percent, arr_weights))
        # Reshape the data and put the percentile dimension
        # back in the right place
        shape = arr_percent.shape + shape
        result = result.reshape(shape)
        # Percentile is now the leading dimension in the result. This needs
        # to move back to where it was in the input data. The result has
        # one less dimension than the original data as we have collapsed
        # one dimension.
        # If we have collapsed a dimension that was before the percentile
        # dimension in the input data, the percentile dimension moves forwards
        # one place compared to the original percentile dimension.
        if axis < perc_dim:
            result = np.moveaxis(result, 0, perc_dim-1)
        # Else we move the percentile dimension back to where it was in the
        # input data, as we have collapsed along a dimension that came after
        # it in the input cube.
        else:
            result = np.moveaxis(result, 0, perc_dim)
        return result

    @staticmethod
    def blend_percentiles(perc_values, percentiles, weights):
        """ Blend percentiles function, to calculate the weighted blend across
            a given axis of percentile data for a single grid point.

        Args:
            perc_values : np.array
                    Array containing the percentile values to blend, with
                    shape: (length of coord to blend, num of percentiles)
            percentiles: np.array
                    Array of percentile values e.g
                    [0, 20.0, 50.0, 70.0, 100.0],
                    same size as the percentile dimension of data.
            weights: np.array
                    Array of weights, same size as the axis dimension of data,
                    that we will blend over.

        Returns:
            result : np.array
                    containing the weighted percentile blend data
                    across the chosen coord
        """
        # Find the size of the dimension we want to blend over.
        num = perc_values.shape[0]
        # Create an array to store the weighted blending pdf
        combined_pdf = np.zeros((num, len(percentiles)))
        # Loop over the axis we are blending over finding the values for the
        # probability at each threshold in the pdf, for each of the other
        # points in the axis we are blending over. Use the values from the
        # percentiles if we are at the same point, otherwise use linear
        # interpolation.
        # Then add the probabilities multiplied by the correct weight to the
        # running total.
        for i in range(0, num):
            for j in range(0, num):
                if i == j:
                    recalc_values_in_pdf = percentiles
                else:
                    recalc_values_in_pdf = np.interp(perc_values[i],
                                                     perc_values[j],
                                                     percentiles)
                # Add the resulting probabilities multiplied by the right
                # weight to the running total for the combined pdf.
                combined_pdf[i] += recalc_values_in_pdf*weights[j]

        # Combine and sort the threshold values for all the points
        # we are blending.
        combined_perc_thres_data = np.sort(perc_values.flatten())

        # Combine and sort blended probability values.
        combined_perc_values = np.sort(combined_pdf.flatten())

        # Find the percentile values from this combined data by interpolating
        # back from probability values to the original percentiles.
        new_combined_perc = np.interp(percentiles,
                                      combined_perc_values,
                                      combined_perc_thres_data)
        return new_combined_perc


class WeightedBlendAcrossWholeDimension(object):
    """Apply a Weighted blend to a cube,
       collapsing across the whole dimension."""

    def __init__(self, coord, coord_adjust=None):
        """Set up for a Weighted Blending plugin

        Args:
            coord : string
                     The name of a coordinate dimension in the cube.
            coord_adjust : Function to apply to the coordinate after
                           collapsing the cube to correct the values,
                           for example for time windowing and
                           cycle averaging the follow function would
                           adjust the time coordinates.
            e.g. coord_adjust = lambda pnts: pnts[len(pnts)/2]
        """
        self.coord = coord
        self.coord_adjust = coord_adjust

    def __repr__(self):
        """Represent the configured plugin instance as a string."""
        return (
            '<WeightedBlendAcrossWholeDimension:'
            ' coord = {0:s}>').format(self.coord)

    def process(self, cube, weights=None):
        """Calculate weighted blend across the chosen coord, for either
           probabilistic or percentile data. If there is a percentile
           coordinate on the cube, it will blend using the
           PercentileBlendingAggregator but the percentile coordinate must
           have at least two points.

        Args:
            cube : iris.cube.Cube
                   Cube to blend across the coord.
            weights: Optional list or np.array of weights
                     or None (equivalent to equal weights).

        Returns:
            result : iris.cube.Cube
                     containing the weighted blend across the chosen coord.

        Raises:
            ValueError : If the first argument not a cube.
            ValueError : If there is a percentile coord and it is not a
                           dimension coord in the cube.
            ValueError : If there is a percentile dimension with only one
                            point, we need at least two points in order to do
                            the blending.
            ValueError : If there are more than one percentile coords
                           in the cube.
            ValueError : If the weights shape do not match the dimension
                           of the coord we are blending over.
        Warns:
            Warning : If trying to blend across a scalar coordinate with only
                        one value. Returns the original cube in this case.

        """
        if not isinstance(cube, iris.cube.Cube):
            msg = ('The first argument must be an instance of '
                   'iris.cube.Cube but is'
                   ' {0:s}.'.format(type(cube)))
            raise ValueError(msg)

        # Check to see if the data is percentile data
        perc_coord = None
        perc_dim = None
        perc_found = 0
        for coord in cube.coords():
            if coord.name().find('percentile') >= 0:
                perc_found += 1
                perc_coord = coord
        if perc_found == 1:
            perc_dim = cube.coord_dims(perc_coord.name())
            if not perc_dim:
                msg = ('The percentile coord must be a dimension '
                       'of the cube.')
                raise ValueError(msg)
            # Check the percentile coordinate has more than one point,
            # otherwise raise an error as we won't be able to blend.
            if len(perc_coord.points) < 2.0:
                msg = ('Percentile coordinate does not have enough points'
                       ' in order to blend. Must have at least 2 percentiles.')
                raise ValueError(msg)
        elif perc_found > 1:
            msg = ('There should only be one percentile coord '
                   'on the cube.')
            raise ValueError(msg)

        # check weights array matches coordinate shape if not None
        if weights is not None:
            if np.array(weights).shape != cube.coord(self.coord).points.shape:
                msg = ('The weights array must match the shape '
                       'of the coordinate in the input cube; '
                       'weight shape is '
                       '{0:s}'.format(np.array(weights).shape) +
                       ', cube shape is '
                       '{0:s}'.format(cube.coord(self.coord).points.shape))
                raise ValueError(msg)

        # If coord to blend over is a scalar_coord warn
        # and return original cube.
        coord_dim = cube.coord_dims(self.coord)
        if not coord_dim:
            msg = ('Trying to blend across a scalar coordinate with only one'
                   ' value. Returning original cube')
            warnings.warn(msg)
            result = cube

        # Blend the cube across the coordinate
        # Use percentile Aggregator if required
        elif perc_coord is not None:
            percentiles = np.array(perc_coord.points, dtype=float)
            perc_dim, = cube.coord_dims(perc_coord.name())
            # Set equal weights if none are provided
            if weights is None:
                num = len(cube.coord(self.coord).points)
                weights = np.ones(num) / float(num)
            # Set up aggregator
            PERCENTILE_BLEND = (Aggregator('percentile_blend',
                                PercentileBlendingAggregator.aggregate))

            result = cube.collapsed(self.coord,
                                    PERCENTILE_BLEND,
                                    arr_percent=percentiles,
                                    arr_weights=weights,
                                    perc_dim=perc_dim)

        # Else do a simple weighted average
        else:
            # Equal weights are used as default.
            weights_array = None
            # Else broadcast the weights to be used by the aggregator.
            if weights is not None:
                weights_array = iris.util.broadcast_to_shape(np.array(weights),
                                                             cube.shape,
                                                             coord_dim)
            # Calculate the weighted average.
            result = cube.collapsed(self.coord,
                                    iris.analysis.MEAN, weights=weights_array)

        # If set adjust values of collapsed coordinates.
        if self.coord_adjust is not None:
            for crd in result.coords():
                if cube.coord_dims(crd.name()) == coord_dim:
                    pnts = cube.coord(crd.name()).points
                    crd.points = np.array(self.coord_adjust(pnts),
                                          dtype=crd.points.dtype)

        return result


class TriangularWeightedBlendAcrossAdjacentPoints(object):
    """
    Apply a Weighted blend to a coordinate, using triangular weights at each
    point in the coordinate.
    Returns a cube with the same coordinates as the input cube, with each
    point in the coordinate of interest having been blended with the adjacent
    points according to a triangular weighting
    function of a specified width.
    """

    def __init__(self, coord, width, parameter_units):
        """Set up for a Weighted Blending plugin

        Args:
            coord : string
                The name of a coordinate dimension in the cube that we
                will blend over.
            width : float
                The width of the triangular weighting function we will use
                to blend.
            parameter_units : string
                The units of the width of the triangular weighting function.
                This does not need to be the same as the units of the
                coordinate we are blending over, but it should be possible to
                convert between them.

        """
        self.coord = coord
        self.width = width
        self.parameter_units = parameter_units

    def __repr__(self):
        """Represent the configured plugin instance as a string."""
        return (
            '<TriangularWeightedBlendAcrossAdjacentPoints:'
            ' coord = {0:s}, width = {1:.2f},'
            ' parameter_units = {2:s}>').format(self.coord, self.width,
                                                self.parameter_units)

    @staticmethod
    def correct_collapsed_coordinates(orig_cube, new_cube, coords_to_correct):
        """
        A helper function to replace the points and bounds in coordinates
        that have been collapsed.
        For the coordinates specified it replaces points in new_cube's
        coordinates with the points from the corresponding coordinate in
        orig_cube. The bounds are also replaced.

        Args:
            orig_cube: iris.cube.Cube
                The cube that the original coordinates points will be taken
                from.
            new_cube: iris.cube.Cube
                The new cube who's coordinates will be corrected. This must
                have the same number of points along the coordinates we are
                correcting as are in the orig_cube.
            coords_to_correct: list
                A list of coordinate names to correct.
        """
        for coord in coords_to_correct:
            new_coord = new_cube.coord(coord)
            old_coord = orig_cube.coord(coord)
            new_coord.points = old_coord.points
            if old_coord.bounds is not None:
                new_coord.bounds = old_coord.bounds

    def process(self, cube):
        """
        Apply the weighted blend for each point in the given coordinate.

        Args:
            cube : iris.cube.Cube
                Cube to blend.

        Returns:
            cube: iris.cube.Cube
                The processed cube, with the same coordinates as the input
                cube. The points in one coordinate will be blended with the
                adjacent points based on a triangular weighting function of the
                specified width.

        """
        # We need to correct all the coordinates associated with the dimension
        # we are collapsing over, so find the relevant coordinates now.
        dimension_to_collapse = cube.coord_dims(self.coord)
        coords_to_correct = cube.coords(dimensions=dimension_to_collapse)
        coords_to_correct = [coord.name() for coord in coords_to_correct]
        # We will also need to correct the bounds on these coordinates,
        # as bounds will be added when the blending happens, so add bounds if
        # it doesn't have some already.
        for coord in coords_to_correct:
            cube.coord(coord).guess_bounds()
        # Set up a plugin to calculate the triangular weights.
        WeightsPlugin = ChooseDefaultWeightsTriangular(
            self.width, units=self.parameter_units)
        # Set up the blending function.
        BlendingPlugin = WeightedBlendAcrossWholeDimension(self.coord)
        result = iris.cube.CubeList([])
        # Loop over each point in the coordinate we are blending over, and
        # calculate a new weighted average for it.
        for cube_slice in cube.slices_over(self.coord):
            point = cube_slice.coord(self.coord).points[0]
            weights = WeightsPlugin.process(cube, self.coord, point)
            blended_cube = BlendingPlugin.process(cube, weights)
            self.correct_collapsed_coordinates(cube_slice, blended_cube,
                                               coords_to_correct)
            result.append(blended_cube)
        result = concatenate_cubes(result)
        return result
