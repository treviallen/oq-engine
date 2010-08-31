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

package org.gem.engine.risk.io.reader;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;

import org.gem.engine.risk.core.Site;
import org.gem.engine.risk.core.reader.AssetReader;
import org.gem.engine.risk.core.reader.ExposureReader;
import org.gem.engine.risk.io.reader.ESRIBinaryFileAssetReader;
import org.gem.engine.risk.io.reader.definition.ESRIRasterFileDefinition;
import org.gem.engine.risk.io.reader.definition.GridDefinition;
import org.junit.Before;
import org.junit.Test;

public class ESRIBinaryFileAssetReaderTest implements ExposureReader
{

    private static final double NO_DATA = -9999.0;

    private Site site;
    private double value;
    private AssetReader reader;

    @Before
    public void setUp()
    {
        site = new Site(0.0, 0.0);
        GridDefinition gridDefinition = new GridDefinition(0, 0, (int) NO_DATA);
        ESRIRasterFileDefinition definition = new ESRIRasterFileDefinition(null, 0.0, gridDefinition);
        reader = new ESRIBinaryFileAssetReader(this, definition);
    }

    @Test
    public void shouldLoadTheAssetValueFromTheExposure()
    {
        value = 111.0;
        assertTrue(reader.readAt(site).isComputable());
        assertEquals(111.0, reader.readAt(site).getValue(), 0.0);
    }

    @Test
    public void shouldResultInAnEmptyAssetIfTheExposureHasNoData()
    {
        value = NO_DATA;
        assertFalse(reader.readAt(site).isComputable());
    }

    @Test
    public void shouldLinkTheSiteWhereDefined()
    {
        value = NO_DATA;
        assertEquals(site, reader.readAt(site).definedAt());
    }

    @Override
    public double readAt(Site site)
    {
        return value;
    }

}
