/*
 * [COPYRIGHT]
 *
 * [NAME] is free software; you can redistribute it and/or modify it
 * under the terms of the GNU Lesser General Public License as
 * published by the Free Software Foundation; either version 2.1 of
 * the License, or (at your option) any later version.
 *
 * This software is distributed in the hope that it will be useful,
 * but WITHOUT ANY WARRANTY; without even the implied warranty of
 * MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU
 * Lesser General Public License for more details.
 *
 * You should have received a copy of the GNU Lesser General Public
 * License along with this software; if not, write to the Free
 * Software Foundation, Inc., 51 Franklin St, Fifth Floor, Boston, MA
 * 02110-1301 USA, or see the FSF site: http://www.fsf.org.
 */

package org.gem.engine.risk.core.validation;

import static org.gem.engine.risk.core.AdditionalPipeKeys.MEAN_FUNCTION;
import static org.gem.engine.risk.core.AdditionalPipeKeys.MEAN_IML;

import org.gem.engine.risk.core.cache.Pipe;
import org.gem.engine.risk.data.DiscreteVulnerabilityFunction;

/**
 * Checks if a previously loaded hazard IML value is inside the range defined by the loaded {@link DiscreteVulnerabilityFunction}.
 * 
 * @author Andrea Cerisara
 * @version $Id: IsHazardIMLInRange.java 499 2010-05-18 12:51:39Z acerisara $
 */
public class IsHazardIMLInRange extends Specification
{

    @Override
    public boolean isSatisfiedBy(Pipe pipe)
    {
        Double IML = pipe.get(MEAN_IML);
        DiscreteVulnerabilityFunction function = pipe.get(MEAN_FUNCTION);

        return !function.outOfBounds(IML);
    }

}
